"""后台「会话列表 / 会话详情 / 证据导出」接口（PRD §4.4）。

与 `admin_logs.py` 的区别：那里是**消息级**问答日志（一轮一行），这里是**会话级**聚合
（一次访问一行），因为举证材料是按会话打包的，不是按单条消息。

三条路径：
- `GET /admin/conversations`：按国家/语言/转人工原因/关键字筛选 + 分页
- `GET /admin/conversations/{id}`：详情（消息流 + 引用 + 工具调用 + 转人工）
- `GET /admin/conversations/{id}/export`：JSON / Markdown 证据包（带下载头）
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import export as export_module
from .deps import get_conn, require_governance_admin

router = APIRouter(
    prefix="/admin",
    tags=["admin-governance"],
    dependencies=[Depends(require_governance_admin)],
)

ExportFormat = Literal["json", "md"]


def _not_found(message: str = "会话不存在") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOT_FOUND", "message": message},
    )


@router.get("/conversations")
def list_conversations(
    country: str | None = Query(default=None, max_length=8),
    language: str | None = Query(default=None, max_length=8),
    handoffReason: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """会话级列表：国家/语言/转人工原因/关键字筛选。"""
    clauses: list[str] = []
    params: list[Any] = []
    if country:
        clauses.append("host_context->>'country' = %s")
        params.append(country.upper())
    if language:
        clauses.append("host_context->>'locale' = %s")
        params.append(language.lower())
    if handoffReason:
        clauses.append(
            "exists (select 1 from handoffs h where h.session_id = s.id "
            "and h.context->>'reason' = %s)"
        )
        params.append(handoffReason)
    if q:
        clauses.append(
            "exists (select 1 from messages m where m.session_id = s.id "
            "and m.content ilike %s)"
        )
        params.append(f"%{q}%")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    offset = (page - 1) * pageSize

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select s.id, s.created_at, s.channel, s.host_context,
                   (select count(*) from messages m where m.session_id = s.id) as message_count,
                   (select count(*) from tool_calls t where t.session_id = s.id) as tool_count,
                   (select count(*) from handoffs h where h.session_id = s.id) as handoff_count,
                   (select h.context->>'reason' from handoffs h
                     where h.session_id = s.id order by h.id desc limit 1) as handoff_reason,
                   count(*) over() as total
              from sessions s
              {where}
             order by s.id desc
             limit %s offset %s
            """,
            (*params, pageSize, offset),
        )
        rows = cur.fetchall()

    items = []
    total = 0
    for row in rows:
        (
            session_id,
            created_at,
            channel,
            host_context,
            message_count,
            tool_count,
            handoff_count,
            handoff_reason,
            total,
        ) = row
        context = host_context or {}
        items.append(
            {
                "id": f"ses-{session_id}",
                "createdAt": created_at.isoformat() if created_at else None,
                "channel": channel,
                "country": context.get("country"),
                "locale": context.get("locale"),
                "orderNo": context.get("orderNo"),
                "messageCount": int(message_count or 0),
                "toolCount": int(tool_count or 0),
                "handoffCount": int(handoff_count or 0),
                "handoffReason": handoff_reason,
            }
        )
    return {
        "items": items,
        "pagination": {"page": page, "pageSize": pageSize, "total": int(total or 0)},
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, Any]:
    package = export_module.build_package(conn, conversation_id)
    if package is None:
        raise _not_found()
    return package


@router.get("/conversations/{conversation_id}/export", response_model=None)
def export_conversation(
    conversation_id: str,
    format: ExportFormat = Query(default="json"),
    conn: psycopg.Connection = Depends(get_conn),
) -> Any:
    """证据包下载：JSON（机器可读，含 sha256）或 Markdown（人工可读）。"""
    package = export_module.build_package(conn, conversation_id)
    if package is None:
        raise _not_found()

    safe_name = conversation_id.replace("/", "-")
    if format == "md":
        markdown = export_module.render_markdown(package)
        return PlainTextResponse(
            content=markdown,
            headers={
                "Content-Disposition": f'attachment; filename="conversation-{safe_name}.md"'
            },
        )
    return JSONResponse(
        content=package,
        headers={
            "Content-Disposition": f'attachment; filename="conversation-{safe_name}.json"'
        },
    )
