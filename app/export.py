"""会话证据包（PayPal 纠纷举证用）：JSON（机器可读）+ Markdown（人工可读）。

设计要点：
- **快照而非引用**：导出时把引用切片的原文（截断 500 字）与文档版本/生效日期一起写进去，
  否则半年后知识库改版，这份"证据"就对不上当时到底引了什么；
- **可校验**：JSON 里带 `integrity.sha256`（对去掉该字段后的规范化 JSON 求值），
  任何人拿到文件都能重算，确认没被改过；
- **免责**：虚构演示数据 + 不保证诉讼胜诉，两条声明都写进文件本身（不是只放在页面上）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import psycopg

EXPORT_VERSION = "1.0"
EXCERPT_LIMIT = 500

DISCLAIMER = {
    "fictional": (
        "Fictional demo data: the brand, orders, shipments and policy figures in this "
        "conversation are invented for demonstration. Not a real customer record."
    ),
    "notLegalAdvice": (
        "This package is supporting material for a dispute review. It does not guarantee "
        "the outcome of any PayPal dispute or legal proceeding."
    ),
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _session_pk(session_id: str) -> int | None:
    raw = (session_id or "").strip()
    if raw.startswith("ses-"):
        raw = raw[4:]
    return int(raw) if raw.isdigit() else None


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_package(conn: psycopg.Connection, session_id: str) -> dict[str, Any] | None:
    """读一个会话，生成完整证据包（不含 sha256 的 integrity 字段由调用方补）。"""
    sid = _session_pk(session_id)
    if sid is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "select id, created_at, channel, auth_source, host_context, engineer_id "
            "from sessions where id = %s",
            (sid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        _, created_at, channel, auth_source, host_context, engineer_id = row

        cur.execute(
            """
            select id, role, content, structured_query, evidence_ids, citations,
                   refused, gate_reason, created_at
              from messages where session_id = %s order by id
            """,
            (sid,),
        )
        message_rows = cur.fetchall()

        cur.execute(
            """
            select message_id, tool, args, status, latency_ms, result, created_at
              from tool_calls where session_id = %s order by id
            """,
            (sid,),
        )
        tool_rows = cur.fetchall()

        cur.execute(
            "select message_id, context, created_at from handoffs "
            "where session_id = %s order by id",
            (sid,),
        )
        handoff_rows = cur.fetchall()

        evidence_ids: list[str] = []
        for message in message_rows:
            evidence_ids.extend(message[4] or [])
            for citation in message[5] or []:
                evidence_id = citation.get("evidenceId") if isinstance(citation, dict) else None
                if evidence_id:
                    evidence_ids.append(evidence_id)
        unique_ids = list(dict.fromkeys(evidence_ids))

        evidence: list[dict[str, Any]] = []
        documents: dict[int, dict[str, Any]] = {}
        if unique_ids:
            cur.execute(
                """
                select c.evidence_id, c.content, c.section_no, c.page_from, c.page_to,
                       c.manual_version, d.id, d.title, d.doc_code, d.manual_version,
                       d.effective_date, d.status
                  from chunks c join documents d on d.id = c.document_id
                 where c.evidence_id = any(%s)
                """,
                (unique_ids,),
            )
            for chunk in cur.fetchall():
                (
                    evidence_id,
                    content,
                    section_no,
                    page_from,
                    page_to,
                    chunk_version,
                    doc_id,
                    title,
                    doc_code,
                    doc_version,
                    effective_date,
                    status,
                ) = chunk
                loc_parts = []
                if section_no:
                    loc_parts.append(f"§{section_no}")
                if page_from:
                    loc_parts.append(
                        f"P{page_from}"
                        if not page_to or page_to == page_from
                        else f"P{page_from}–{page_to}"
                    )
                evidence.append(
                    {
                        "evidenceId": evidence_id,
                        "documentTitle": title,
                        "loc": " · ".join(loc_parts) or None,
                        "version": doc_version or chunk_version,
                        "effectiveDate": _iso(effective_date),
                        "contentExcerpt": (content or "")[:EXCERPT_LIMIT],
                        "truncated": len(content or "") > EXCERPT_LIMIT,
                    }
                )
                documents[int(doc_id)] = {
                    "docId": f"doc-{doc_id}",
                    "title": title,
                    "docCode": doc_code,
                    "version": doc_version,
                    "effectiveDate": _iso(effective_date),
                    "status": status,
                }

    tools_by_message: dict[int, list[dict[str, Any]]] = {}
    for tool_row in tool_rows:
        message_id, tool, args, status, latency_ms, result, created = tool_row
        key = int(message_id) if message_id is not None else 0
        tools_by_message.setdefault(key, []).append(
            {
                "tool": tool,
                "args": args,
                "status": status,
                "latencyMs": latency_ms,
                "result": result,
                "createdAt": _iso(created),
            }
        )

    handoffs_by_message: dict[int, dict[str, Any]] = {}
    handoff_list: list[dict[str, Any]] = []
    for handoff_row in handoff_rows:
        message_id, context, created = handoff_row
        item = {
            "messageId": f"msg-{message_id}" if message_id is not None else None,
            "context": context,
            "createdAt": _iso(created),
        }
        handoff_list.append(item)
        if message_id is not None:
            handoffs_by_message[int(message_id)] = item

    messages: list[dict[str, Any]] = []
    for message in message_rows:
        (
            message_id,
            role,
            content,
            structured_query,
            evidence_ids_value,
            citations,
            refused,
            gate_reason,
            message_created,
        ) = message
        messages.append(
            {
                "id": f"msg-{message_id}",
                "role": role,
                "content": content,
                "createdAt": _iso(message_created),
                "refused": bool(refused),
                "gateReason": gate_reason,
                "intent": (structured_query or {}).get("intent"),
                "structuredQuery": structured_query,
                "citations": citations or [],
                "toolCalls": tools_by_message.get(int(message_id), []),
                "handoff": handoffs_by_message.get(int(message_id)),
            }
        )

    context = host_context or {}
    payload: dict[str, Any] = {
        "exportVersion": EXPORT_VERSION,
        "exportedAt": dt.datetime.now().astimezone().isoformat(),
        "disclaimer": dict(DISCLAIMER),
        "session": {
            "id": f"ses-{sid}",
            "createdAt": _iso(created_at),
            "channel": channel,
            "authSource": auth_source,
            "identified": engineer_id is not None,
            "country": context.get("country"),
            "locale": context.get("locale"),
            "orderNo": context.get("orderNo"),
            "hostContext": context,
        },
        "messages": messages,
        "evidence": evidence,
        "documents": list(documents.values()),
        "handoffs": handoff_list,
    }
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    payload["integrity"] = {
        "sha256": digest,
        "algorithm": "sha256",
        "scope": "package-without-integrity",
    }
    return payload


def render_markdown(package: dict[str, Any]) -> str:
    """人工可读版：会话摘要 → 逐轮对话 → 引用原文 → 工具调用 → 转人工 → 声明。"""
    session = package.get("session", {})
    lines: list[str] = [
        "# Customer conversation evidence package",
        "",
        f"> Exported at {package.get('exportedAt')} · package v{package.get('exportVersion')}",
        f"> Session `{session.get('id')}` · channel `{session.get('channel')}` · "
        f"country `{session.get('country') or '—'}` · language `{session.get('locale') or '—'}` · "
        f"order `{session.get('orderNo') or '—'}`",
        "",
        "## Disclaimer",
        "",
    ]
    for key, value in (package.get("disclaimer") or {}).items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")

    lines.extend(["## Conversation", ""])
    for message in package.get("messages") or []:
        role = "Customer" if message.get("role") == "user" else "Assistant"
        flags = []
        if message.get("refused"):
            flags.append(f"refused:{message.get('gateReason') or '-'}")
        if message.get("intent"):
            flags.append(f"intent:{message['intent']}")
        suffix = f" _({', '.join(flags)})_" if flags else ""
        lines.append(f"### {role} · {message.get('createdAt')}{suffix}")
        lines.append("")
        lines.append((message.get("content") or "").strip() or "_(empty)_")
        lines.append("")
        citations = message.get("citations") or []
        if citations:
            lines.append("Citations:")
            for citation in citations:
                lines.append(
                    f"- `{citation.get('evidenceId')}` · {citation.get('title')} · "
                    f"{citation.get('loc') or '—'}"
                )
            lines.append("")
        for tool in message.get("toolCalls") or []:
            lines.append(
                f"- Tool call `{tool.get('tool')}` · {tool.get('status')} · "
                f"{tool.get('latencyMs')} ms · args "
                f"`{json.dumps(tool.get('args') or {}, ensure_ascii=False)}`"
            )
            lines.append("")
        handoff = message.get("handoff")
        if handoff:
            context = handoff.get("context") or {}
            lines.append(
                f"- Handoff: reason `{context.get('reason') or context.get('category') or '—'}` · "
                f"rule `{context.get('rule') or '—'}`"
            )
            lines.append("")

    evidence = package.get("evidence") or []
    if evidence:
        lines.extend(["## Evidence snapshot", ""])
        for item in evidence:
            lines.append(
                f"### `{item.get('evidenceId')}` · {item.get('documentTitle')} · "
                f"v{item.get('version') or '—'} · effective {item.get('effectiveDate') or '—'}"
            )
            lines.append("")
            lines.append(f"> {item.get('loc') or 'location not recorded'}")
            lines.append("")
            lines.append(item.get("contentExcerpt") or "")
            if item.get("truncated"):
                lines.append("")
                lines.append("_(truncated)_")
            lines.append("")

    documents = package.get("documents") or []
    if documents:
        lines.extend(["## Documents used", ""])
        for document in documents:
            code = document.get("docCode") or document.get("docId")
            version = document.get("version") or "—"
            effective = document.get("effectiveDate") or "—"
            status_value = document.get("status") or "—"
            lines.append(
                f"- `{code}` · {document.get('title')} · v{version} · "
                f"effective {effective} · status {status_value}"
            )
        lines.append("")

    lines.extend(
        [
            "## Integrity",
            "",
            f"`sha256: {package.get('integrity', {}).get('sha256')}`",
            "",
            "_Recompute: remove the `integrity` object from the JSON, dump it with sorted keys and "
            "no whitespace, then sha256 the UTF-8 bytes._",
        ]
    )
    return "\n".join(lines)
