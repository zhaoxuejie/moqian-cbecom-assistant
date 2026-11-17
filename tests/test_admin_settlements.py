"""后台「知识沉淀」接口测试（需要数据库）。

钉住四条容易漂的口径：
1. 沉淀只是候选知识：确认前不产生任何 documents/chunks（纪律 8：人工确认才入库）；
2. 确认 = 一份 `source_type='ticket'` 知识源 + 一条证据单元，标题/型号/批次/版本随沉淀带，
   正文与出处定位允许确认时覆盖；
3. 只有 pending 能确认/驳回，重复操作 409，未知 id 404，空正文/空驳回理由 422；
4. 列表的 `stats` 与数据库计数同源（与页脚"共 N 条"一套算法）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.main import create_app
from app.messages import log_assistant_message, log_user_message, touch_resolved
from app.routers.deps import get_conn, get_embedder
from app.security import create_admin_token, hash_password
from app.sessions import create_session
from app.users import create_user, get_user_by_account

pytestmark = requires_db

ACCOUNT = "T-TEST-SETTLE"
PASSWORD = "Unit-Test-Pass-9"
JWT_SECRET = "unit-test-secret-32-bytes-long!!"
TITLE_PREFIX = "沉淀测试候选"

DEFAULT_TEXT = "将制动器间隙调整到 0.3–0.5 mm，复测三次后记录工单。"
DEFAULT_LOC = "工单 ZX-2026-101 · 处置记录"


class _FakeEmbedder:
    """不联网的假向量客户端；`fail=True` 用于验证降级路径。"""

    def __init__(self, *, dim: int = 1024, fail: bool = False) -> None:
        self.dim = dim
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding service unavailable")
        return [[0.001] * self.dim for _ in texts]


@pytest.fixture()
def env(conn):
    """管理员登录态 + 可共享的 DB 连接；用例结束清理自建的沉淀/知识源（不动演示数据）。"""
    created_user = False
    user = get_user_by_account(conn, ACCOUNT)
    if user is None:
        create_user(
            conn,
            account=ACCOUNT,
            name="沉淀测试管理员",
            role="admin",
            password_hash=hash_password(PASSWORD),
        )
        created_user = True
        user = get_user_by_account(conn, ACCOUNT)
    assert user is not None

    settings = Settings(jwt_secret=JWT_SECRET)
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_embedder] = lambda: _FakeEmbedder()
    token, _ = create_admin_token(
        int(user["id"]), ACCOUNT, "admin", Settings(jwt_secret=JWT_SECRET)
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})

    state = SimpleNamespace(client=client, conn=conn, settlements=[])

    def make(**overrides: Any) -> int:
        settlement_id = _insert_settlement(conn, **overrides)
        state.settlements.append(settlement_id)
        return settlement_id

    state.make = make
    yield state

    with conn.cursor() as cur:
        if state.settlements:
            cur.execute(
                "select id, confirmed_document_id from settlements where id = any(%s)",
                (state.settlements,),
            )
            rows = cur.fetchall()
            document_ids = [row[1] for row in rows if row[1] is not None]
            if document_ids:
                cur.execute("delete from documents where id = any(%s)", (document_ids,))
            cur.execute("delete from settlements where id = any(%s)", (state.settlements,))
        if created_user:
            cur.execute("delete from users where id = %s", (user["id"],))
    conn.commit()


def _insert_settlement(
    conn,
    *,
    title: str | None = None,
    channel: str = "ticket",
    status: str = "pending",
    source: str = "ZX-2026-101",
    model: str = "ZX-300",
    batch: str = "B23",
    version: str = "Ver.3",
    submitted_by: str = "李工",
    body: str = DEFAULT_TEXT,
    loc: str | None = DEFAULT_LOC,
    context: list[dict[str, str]] | None = None,
    review_log: list[dict[str, str]] | None = None,
) -> int:
    context = (
        context
        if context is not None
        else [
            {"k": "设备型号", "v": model},
            {"k": "故障现象", "v": "制动器异响"},
            {"k": "处置要点", "v": "调整间隙并复测"},
        ]
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into settlements
                (channel, status, title, source, model, batch, version, submitted_by,
                 body, loc, context, review_log)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            returning id
            """,
            (
                channel,
                status,
                title or f"{TITLE_PREFIX}-{channel}",
                source,
                model,
                batch,
                version,
                submitted_by,
                body,
                loc,
                Jsonb(context),
                Jsonb(review_log or []),
            ),
        )
        settlement_id = int(cur.fetchone()[0])
    conn.commit()
    return settlement_id


def _fetch_settlement(conn, settlement_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from settlements where id = %s", (settlement_id,))
        row = cur.fetchone()
    assert row is not None
    return row


def _settlement_documents(conn, settlement_id: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from documents where meta_json ->> 'settlementId' = %s order by id",
            (f"st-{settlement_id}",),
        )
        return cur.fetchall()


def _chunks_for(conn, document_id: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from chunks where document_id = %s order by id", (document_id,))
        return cur.fetchall()


def _db_status_counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("select status, count(*) from settlements group by status")
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}


def _db_count(conn, clause: str, params: tuple[Any, ...]) -> int:
    with conn.cursor() as cur:
        cur.execute(f"select count(*) from settlements where {clause}", params)
        return int(cur.fetchone()[0])


def _db_channel_count(conn, channel: str) -> int:
    return _db_count(conn, "channel = %s", (channel,))


# ---------- 列表 ----------
def test_list_requires_admin_token(env) -> None:
    env.client.headers.pop("Authorization", None)
    resp = env.client.get("/admin/settlements")
    assert resp.status_code == 401


def test_list_pagination_filters_and_stats(env) -> None:
    pending_ticket = env.make(channel="ticket", title=f"{TITLE_PREFIX}-待确认-工单")
    env.make(channel="favorite", title=f"{TITLE_PREFIX}-待确认-收藏")
    env.make(channel="manual", status="confirmed", title=f"{TITLE_PREFIX}-已入库")
    env.make(channel="ticket", status="rejected", title=f"{TITLE_PREFIX}-已驳回")

    resp = env.client.get("/admin/settlements", params={"pageSize": 100, "withStats": "1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"] == {
        "page": 1,
        "pageSize": 100,
        "total": _db_count(env.conn, "1 = 1", ()),
    }
    assert set(body["stats"]) == {"pending", "confirmed", "rejected", "channels"}
    assert body["stats"] == {
        **{
            key: _db_status_counts(env.conn).get(key, 0)
            for key in ("pending", "confirmed", "rejected")
        },
        "channels": {
            "ticket": _db_channel_count(env.conn, "ticket"),
            "favorite": _db_channel_count(env.conn, "favorite"),
            "manual": _db_channel_count(env.conn, "manual"),
        },
    }

    # 字段契约：id 带 st- 前缀、正文映射为 text、context/reviewLog 是数组
    item = next(row for row in body["items"] if row["id"] == f"st-{pending_ticket}")
    assert item["channel"] == "ticket"
    assert item["status"] == "pending"
    assert item["title"] == f"{TITLE_PREFIX}-待确认-工单"
    assert item["text"] == DEFAULT_TEXT
    assert item["loc"] == DEFAULT_LOC
    assert item["model"] == "ZX-300"
    assert item["batch"] == "B23"
    assert item["version"] == "Ver.3"
    assert item["submittedBy"] == "李工"
    assert item["submittedAt"]
    assert item["context"] and item["context"][0]["k"] == "设备型号"
    assert item["reviewLog"] == []

    # 状态过滤：结果与数据库计数同源
    resp = env.client.get("/admin/settlements", params={"status": "pending", "pageSize": 100})
    pending = resp.json()
    assert all(row["status"] == "pending" for row in pending["items"])
    assert pending["pagination"]["total"] == _db_count(env.conn, "status = %s", ("pending",))
    assert "stats" not in pending  # 未请求 withStats 时不返回

    # 通道过滤
    resp = env.client.get("/admin/settlements", params={"channel": "favorite", "pageSize": 100})
    favorite = resp.json()
    assert favorite["items"]
    assert all(row["channel"] == "favorite" for row in favorite["items"])
    assert favorite["pagination"]["total"] == _db_count(env.conn, "channel = %s", ("favorite",))

    # 分页边界
    resp = env.client.get("/admin/settlements", params={"pageSize": 1, "page": 2})
    page2 = resp.json()
    assert page2["pagination"]["pageSize"] == 1
    assert page2["pagination"]["page"] == 2
    assert len(page2["items"]) <= 1


def test_list_rejects_unknown_enum(env) -> None:
    resp = env.client.get("/admin/settlements", params={"status": "archived"})
    assert resp.status_code == 422


# ---------- 确认入库 ----------
def test_confirm_creates_ticket_document_and_chunk(env) -> None:
    settlement_id = env.make()
    # 确认前不产生任何知识源 / 证据单元（纪律 8）
    assert _settlement_documents(env.conn, settlement_id) == []

    resp = env.client.post(
        f"/admin/settlements/st-{settlement_id}/confirm",
        json={"note": "核对无误，来源可追溯"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["text"] == DEFAULT_TEXT
    assert body["loc"] == DEFAULT_LOC
    assert body["reviewLog"][-1]["action"] == "confirmed"
    assert body["reviewLog"][-1]["note"] == "核对无误，来源可追溯"
    assert body["reviewLog"][-1]["by"]
    assert body["vectorization"]["status"] == "ready"
    assert body["vectorization"]["dim"] == 1024

    settlement = _fetch_settlement(env.conn, settlement_id)
    document_id = settlement["confirmed_document_id"]
    chunk_id = settlement["confirmed_chunk_id"]
    assert document_id is not None and chunk_id is not None

    documents = _settlement_documents(env.conn, settlement_id)
    assert len(documents) == 1
    document = documents[0]
    assert document["source_type"] == "ticket"
    assert document["title"] == f"{TITLE_PREFIX}-ticket"
    assert document["device_model"] == "ZX-300"
    assert document["batch"] == "B23"
    assert document["manual_version"] == "Ver.3"
    assert document["status"] == "parsed"
    assert document["meta_json"]["source"] == "ZX-2026-101"
    assert document["meta_json"]["settlementId"] == f"st-{settlement_id}"
    assert document["meta_json"]["category"] == "ticket"

    chunks = _chunks_for(env.conn, document_id)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["id"] == chunk_id
    assert chunk["evidence_id"].startswith("T")
    assert chunk["content"] == DEFAULT_TEXT
    assert chunk["meta_json"]["loc"] == DEFAULT_LOC
    assert chunk["bm25_tokens"], "BM25 需要 jieba 预分词才能命中"
    assert chunk["embedding"] is not None, "确认入库应补查询向量（D-013）"
    assert "0.001" in str(chunk["embedding"])
    assert chunk["batch"] == "B23"
    assert chunk["manual_version"] == "Ver.3"


def test_confirm_survives_embedding_failure(env) -> None:
    """向量服务失败时，确认入库仍成功，切片保留 NULL 并返回 failed 状态。"""
    env.client.app.dependency_overrides[get_embedder] = lambda: _FakeEmbedder(fail=True)
    try:
        settlement_id = env.make()
        resp = env.client.post(
            f"/admin/settlements/st-{settlement_id}/confirm",
            json={"note": "向量降级测试"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "confirmed"
        assert body["vectorization"]["status"] == "failed"
        settlement = _fetch_settlement(env.conn, settlement_id)
        chunk = _chunks_for(env.conn, settlement["confirmed_document_id"])[0]
        assert chunk["embedding"] is None
        assert chunk["bm25_tokens"]
    finally:
        env.client.app.dependency_overrides[get_embedder] = lambda: _FakeEmbedder()


def test_confirm_allows_text_and_loc_override(env) -> None:
    settlement_id = env.make()
    resp = env.client.post(
        f"/admin/settlements/st-{settlement_id}/confirm",
        json={
            "text": "  修正后的确认正文：先断电，再调整间隙。  ",
            "loc": "工单 ZX-2026-101 · 第 2 段",
            "note": "补充断电前置条件",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "修正后的确认正文：先断电，再调整间隙。"
    assert body["loc"] == "工单 ZX-2026-101 · 第 2 段"

    settlement = _fetch_settlement(env.conn, settlement_id)
    chunk = _chunks_for(env.conn, settlement["confirmed_document_id"])[0]
    assert chunk["content"] == "修正后的确认正文：先断电，再调整间隙。"
    assert chunk["meta_json"]["loc"] == "工单 ZX-2026-101 · 第 2 段"


def test_confirm_empty_body_is_validation_error(env) -> None:
    settlement_id = env.make(body="   ")
    resp = env.client.post(f"/admin/settlements/st-{settlement_id}/confirm", json={})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert _fetch_settlement(env.conn, settlement_id)["status"] == "pending"
    assert _settlement_documents(env.conn, settlement_id) == []


def test_confirm_non_pending_is_conflict(env) -> None:
    settlement_id = env.make()
    first = env.client.post(f"/admin/settlements/st-{settlement_id}/confirm", json={})
    assert first.status_code == 200

    second = env.client.post(f"/admin/settlements/st-{settlement_id}/confirm", json={})
    assert second.status_code == 409
    assert second.json()["code"] == "CONFLICT"
    # 重复确认不能生成第二份知识源
    assert len(_settlement_documents(env.conn, settlement_id)) == 1

    rejected_id = env.make(status="rejected")
    resp = env.client.post(f"/admin/settlements/st-{rejected_id}/confirm", json={})
    assert resp.status_code == 409


def test_confirm_unknown_is_404(env) -> None:
    resp = env.client.post("/admin/settlements/st-999999999/confirm", json={})
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    resp = env.client.post("/admin/settlements/not-a-number/confirm", json={})
    assert resp.status_code == 404


def test_evidence_ids_are_unique_across_confirmations(env) -> None:
    first_id = env.make(title=f"{TITLE_PREFIX}-A")
    second_id = env.make(title=f"{TITLE_PREFIX}-B")
    assert env.client.post(f"/admin/settlements/st-{first_id}/confirm", json={}).status_code == 200
    assert env.client.post(f"/admin/settlements/st-{second_id}/confirm", json={}).status_code == 200

    evidence_ids = []
    for settlement_id in (first_id, second_id):
        settlement = _fetch_settlement(env.conn, settlement_id)
        chunk = _chunks_for(env.conn, settlement["confirmed_document_id"])[0]
        evidence_ids.append(chunk["evidence_id"])
    assert evidence_ids[0] != evidence_ids[1]
    assert all(value.startswith("T") for value in evidence_ids)


# ---------- 驳回 ----------
def test_reject_requires_note(env) -> None:
    settlement_id = env.make()
    for payload in ({}, {"note": "   "}):
        resp = env.client.post(f"/admin/settlements/st-{settlement_id}/reject", json=payload)
        assert resp.status_code == 422
        assert resp.json()["code"] == "VALIDATION_ERROR"
    assert _fetch_settlement(env.conn, settlement_id)["status"] == "pending"


def test_reject_pending_marks_rejected_with_review_log(env) -> None:
    settlement_id = env.make()
    resp = env.client.post(
        f"/admin/settlements/st-{settlement_id}/reject",
        json={"note": "与手册 §6.4.3 冲突，不得仅复位运行"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reviewLog"][-1]["action"] == "rejected"
    assert body["reviewLog"][-1]["note"] == "与手册 §6.4.3 冲突，不得仅复位运行"
    assert body["reviewLog"][-1]["by"]

    settlement = _fetch_settlement(env.conn, settlement_id)
    assert settlement["status"] == "rejected"
    assert settlement["confirmed_document_id"] is None
    assert settlement["confirmed_chunk_id"] is None
    assert _settlement_documents(env.conn, settlement_id) == []

    second = env.client.post(
        f"/admin/settlements/st-{settlement_id}/reject", json={"note": "再驳一次"}
    )
    assert second.status_code == 409


def test_reject_unknown_is_404(env) -> None:
    resp = env.client.post("/admin/settlements/st-999999999/reject", json={"note": "不存在"})
    assert resp.status_code == 404


# ---------- 候选生产端：收藏 / 工单（D-020） ----------
WIDGET_HEADER = "X-Widget-Session"
TICKET = "T-TEST-WO-9200"


@pytest.fixture()
def sources(env):
    """自建会话 + 候选；用例结束清理，绝不碰演示数据。"""
    state = SimpleNamespace(sessions=[], settlements=[])
    state.widget = TestClient(env.client.app)

    def make_session(*, host=None, channel="widget", claimed=ACCOUNT):
        created = create_session(
            env.conn,
            requested_source="host_injected",
            claimed_account=claimed,
            host_context=host or {},
            channel=channel,
        )
        state.sessions.append(created["sessionId"])
        return created["sessionId"]

    state.make_session = make_session
    yield state

    with env.conn.cursor() as cur:
        if state.settlements:
            cur.execute("delete from settlements where id = any(%s)", (state.settlements,))
        if state.sessions:
            pks = [int(item.replace("ses-", "")) for item in state.sessions]
            cur.execute("delete from sessions where id = any(%s)", (pks,))
    env.conn.commit()


def _settlement_by_origin(conn, channel: str, origin_ref: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from settlements where channel = %s and origin_ref = %s "
            "order by id desc limit 1",
            (channel, origin_ref),
        )
        return cur.fetchone()


def _save_favorite(widget_client, session_id: str, message_id: str):
    return widget_client.post(
        "/widget/feedback",
        json={"sessionId": session_id, "messageId": message_id, "type": "saved"},
        headers={WIDGET_HEADER: session_id},
    )


def test_favorite_feedback_creates_pending_candidate(env, sources) -> None:
    """收藏回答 = 生成待确认候选：正文去引用标记、来源可追溯、重复收藏不重复建。"""
    sid = sources.make_session(
        host={"ticketNo": TICKET, "deviceModel": "BK系列", "batch": "B22"}
    )
    log_user_message(env.conn, session_id=sid, question="制动器间隙怎么调？")
    message_id = log_assistant_message(
        env.conn,
        session_id=sid,
        content="用塞尺测量间隙 [M37004]，调整到 0.3–0.5 mm 后复测。",
        evidence_ids=["M37004"],
        citations=[{"evidenceId": "M37004", "kind": "manual"}],
    )
    assert message_id is not None

    resp = _save_favorite(sources.widget, sid, message_id)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    row = _settlement_by_origin(env.conn, "favorite", f"msg-{message_id[4:]}")
    assert row is not None
    sources.settlements.append(int(row["id"]))
    assert row["status"] == "pending"
    assert row["title"] == "制动器间隙怎么调？"
    assert row["body"] == "用塞尺测量间隙，调整到 0.3–0.5 mm 后复测。"
    assert "[M37004]" not in row["body"]
    assert row["source"] == f"会话 {sid} · {message_id}"
    assert row["model"] == "BK系列"
    assert row["batch"] == "B22"
    assert row["submitted_by"] == "沉淀测试管理员"
    context = {item["k"]: item["v"] for item in row["context"]}
    assert context["问题"] == "制动器间隙怎么调？"
    assert context["订单号"] == TICKET
    assert context["回答类型"] == "正常作答"
    # 纪律 8：候选只是候选，确认前不产生知识源 / 切片
    assert _settlement_documents(env.conn, row["id"]) == []

    # 同一回答重复收藏（例如网络重试）不重复建候选
    assert _save_favorite(sources.widget, sid, message_id).status_code == 200
    with env.conn.cursor() as cur:
        cur.execute(
            "select count(*) from settlements where channel = 'favorite' and origin_ref = %s",
            (f"msg-{message_id[4:]}",),
        )
        assert int(cur.fetchone()[0]) == 1

    # 收藏记录本身仍按原口径落库（我的收藏数据源不变）
    with env.conn.cursor() as cur:
        cur.execute("select feedback from messages where id = %s", (int(message_id[4:]),))
        assert cur.fetchone()[0] == "saved"


def test_favorite_from_admin_preview_is_ignored(env, sources) -> None:
    """管理自测会话不进沉淀队列（与「不进任何指标」同一口径）。"""
    sid = sources.make_session(channel="admin_preview", host={"deviceModel": "BK系列"})
    log_user_message(env.conn, session_id=sid, question="自测问题")
    message_id = log_assistant_message(env.conn, session_id=sid, content="自测回答")
    assert message_id is not None

    assert _save_favorite(sources.widget, sid, message_id).status_code == 200
    assert _settlement_by_origin(env.conn, "favorite", f"msg-{message_id[4:]}") is None


def test_ticket_settlement_aggregates_resolved_rounds(env, sources) -> None:
    """工单完成 → 候选：按工单号跨会话聚合已解决轮次，重复触发幂等。"""
    sid_a = sources.make_session(
        host={"ticketNo": TICKET, "deviceModel": "BK系列", "batch": "B22"}
    )
    log_user_message(env.conn, session_id=sid_a, question="门锁闭合不到位怎么处理？")
    log_assistant_message(
        env.conn, session_id=sid_a, content="调整门锁触点位置 [M37002]，复测三次。"
    )
    touch_resolved(env.conn, session_id=sid_a, resolved=True)

    sid_b = sources.make_session(
        host={"ticketNo": TICKET, "deviceModel": "BK系列", "batch": "B22"}
    )
    log_user_message(env.conn, session_id=sid_b, question="层门间隙标准是多少？")
    log_assistant_message(env.conn, session_id=sid_b, content="层门间隙按手册 §4.2 执行。")
    touch_resolved(env.conn, session_id=sid_b, resolved=True)

    resp = env.client.post(
        "/admin/settlements/from-ticket",
        json={"sessionId": sid_a, "note": "工单已闭环"},
    )
    assert resp.status_code == 200
    body = resp.json()
    sources.settlements.append(int(body["id"].replace("st-", "")))
    assert body["created"] is True
    assert body["channel"] == "ticket"
    assert body["status"] == "pending"
    assert body["source"] == TICKET
    assert body["title"].startswith(f"会话 {TICKET} · ")
    assert "【客户问题】门锁闭合不到位怎么处理？" in body["text"]
    assert "【回复要点】调整门锁触点位置，复测三次。" in body["text"]
    assert "[M37002]" not in body["text"]
    assert "【客户问题】层门间隙标准是多少？" in body["text"]
    context = {item["k"]: item["v"] for item in body["context"]}
    assert context["已解决轮次"] == "2"
    assert context["触发人"]
    assert context["触发备注"] == "工单已闭环"
    assert sid_a in context["涉及会话"] and sid_b in context["涉及会话"]
    assert _settlement_documents(env.conn, int(body["id"].replace("st-", ""))) == []

    # 幂等：再点一次返回同一条候选
    again = env.client.post(
        "/admin/settlements/from-ticket", json={"sessionId": sid_b}
    )
    assert again.status_code == 200
    assert again.json()["id"] == body["id"]
    assert again.json()["created"] is False

    # 驳回后允许重新提交：生成新候选，旧条目保留为已驳回
    rejected = env.client.post(
        f"/admin/settlements/{body['id']}/reject", json={"note": "与手册 §4.2 冲突"}
    )
    assert rejected.status_code == 200
    retry = env.client.post(
        "/admin/settlements/from-ticket", json={"sessionId": sid_a}
    )
    assert retry.status_code == 200
    assert retry.json()["created"] is True
    assert retry.json()["id"] != body["id"]
    sources.settlements.append(int(retry.json()["id"].replace("st-", "")))


def test_ticket_without_order_or_resolved_round_conflicts(env, sources) -> None:
    """没有订单号也能按会话沉淀；但没有已解决轮次必须 409（不再要求 ticketNo）。"""
    sid = sources.make_session(host={})
    resp = env.client.post(
        "/admin/settlements/from-ticket", json={"sessionId": sid}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


def test_ticket_requires_resolved_round(env, sources) -> None:
    ticket = TICKET + "-OPEN"
    sid = sources.make_session(host={"ticketNo": ticket})
    log_user_message(env.conn, session_id=sid, question="还在排查中")
    log_assistant_message(env.conn, session_id=sid, content="先观察运行状态。")

    resp = env.client.post(
        "/admin/settlements/from-ticket", json={"sessionId": sid}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"
    assert _settlement_by_origin(env.conn, "ticket", f"ticket:{ticket}") is None


def test_ticket_unknown_session_is_404(env) -> None:
    resp = env.client.post(
        "/admin/settlements/from-ticket", json={"sessionId": "ses-999999999"}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
