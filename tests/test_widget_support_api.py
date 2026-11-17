"""Widget 支撑接口（需要数据库）：引用原文 / 反馈 / 转人工 / 配置开关。

钉住三个安全与口径约束：
- 引用原文只允许取**本会话引用过**的证据，否则 404（不能拿别的会话翻库）；
- 反馈必须带本会话的 messageId，`resolved` 要同时写 resolved_at（定位时长终点）；
- 转人工返回的上下文与真实会话一致（轮次/引用数/转人工次数），不编造。
"""

from __future__ import annotations

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.messages import log_assistant_message, log_user_message
from app.routers.deps import get_conn
from app.sessions import create_session

pytestmark = requires_db

SESSION_HEADER = "X-Widget-Session"


@pytest.fixture()
def seeded(conn):
    # 引用原文的"跨会话不可取"语义要拿两条**真实**证据单元做样本；原件不入版本库
    # （D-029 / data-layout §5），CI 的 data/ 是空的、chunks 表没有行，先判定再建会话。
    with conn.cursor() as cur:
        cur.execute("select evidence_id from chunks where disabled = false order by id limit 2")
        rows = cur.fetchall()
    if len(rows) < 2:
        pytest.skip(
            "需要至少 2 条真实证据单元（本机先 python -m app.import_docs）；CI 无 data/ 自动跳过"
        )
    cited_id, uncited_id = rows[0][0], rows[1][0]

    session = create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"ticketNo": "WO-2026-001", "deviceModel": "BK系列", "batch": "B22"},
    )
    session_id = session["sessionId"]

    log_user_message(conn, session_id=session_id, question="制动器间隙怎么调？")
    message_id = log_assistant_message(
        conn,
        session_id=session_id,
        content=f"1. 用塞尺测量间隙 [{cited_id}]",
        evidence_ids=[cited_id],
        citations=[
            {
                "evidenceId": cited_id,
                "kind": "manual",
                "title": "《维修手册》",
                "loc": "§3.2 · P6",
            }
        ],
    )
    yield {
        "session_id": session_id,
        "message_id": message_id,
        "cited_id": cited_id,
        "uncited_id": uncited_id,
    }
    session_pk = int(session_id.replace("ses-", ""))
    with conn.cursor() as cur:
        cur.execute("delete from handoffs where session_id = %s", (session_pk,))
        cur.execute("delete from sessions where id = %s", (session_pk,))
    conn.commit()


@pytest.fixture()
def client(conn):
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = get_settings
    return TestClient(app)


def test_widget_config_exposes_s4_features(client) -> None:
    body = client.get("/widget/config").json()
    assert body["features"]["evidence"] is True
    assert body["features"]["feedback"] is True
    assert body["features"]["handoff"] is True
    assert body["welcome"]


def test_evidence_returns_cited_text_with_provenance(client, seeded) -> None:
    resp = client.get(
        f"/widget/evidence/{seeded['cited_id']}", headers={SESSION_HEADER: seeded["session_id"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["evidenceId"] == seeded["cited_id"]
    assert body["text"]
    assert body["kind"] in {"manual", "faq", "ticket", "drawing", "excel", "video"}
    assert body["loc"], "引用原文必须带出处定位"
    assert body["pageFrom"] is None or isinstance(body["pageFrom"], int)


def test_evidence_rejects_uncited_or_other_session(client, seeded) -> None:
    resp = client.get(
        f"/widget/evidence/{seeded['uncited_id']}",
        headers={SESSION_HEADER: seeded["session_id"]},
    )
    assert resp.status_code == 404, "未被本会话引用的证据不能取原文"
    assert resp.json()["code"] == "EVIDENCE_NOT_FOUND"

    missing = client.get(f"/widget/evidence/{seeded['cited_id']}")
    assert missing.status_code == 401


def test_feedback_resolves_message(client, conn, seeded) -> None:
    resp = client.post(
        "/widget/feedback",
        headers={SESSION_HEADER: seeded["session_id"]},
        json={
            "sessionId": seeded["session_id"],
            "messageId": seeded["message_id"],
            "type": "resolved",
            "note": "按步骤处理好了",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    with conn.cursor() as cur:
        cur.execute(
            "select feedback, feedback_note, resolved, resolved_at "
            "from messages where id = %s",
            (int(seeded["message_id"].replace("msg-", "")),),
        )
        feedback, note, resolved, resolved_at = cur.fetchone()
    assert feedback == "resolved"
    assert note == "按步骤处理好了"
    assert resolved is True and resolved_at is not None


def test_feedback_rejects_unknown_message(client, seeded) -> None:
    resp = client.post(
        "/widget/feedback",
        headers={SESSION_HEADER: seeded["session_id"]},
        json={"sessionId": seeded["session_id"], "messageId": "msg-999999", "type": "saved"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "MESSAGE_NOT_FOUND"


def test_handoff_returns_honest_context_and_counts(client, conn, seeded) -> None:
    def handoff() -> dict:
        resp = client.post(
            "/widget/handoff",
            headers={SESSION_HEADER: seeded["session_id"]},
            json={"sessionId": seeded["session_id"], "messageId": seeded["message_id"]},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    first = handoff()
    assert first["hotline"]
    assert first["hours"]
    assert first["ticketNo"] == "WO-2026-001"
    context = first["context"]
    assert context["deviceModel"] == "BK系列"
    assert context["batch"] == "B22"
    assert context["rounds"] == 1
    assert context["citedCount"] == 1
    assert context["handoffCount"] == 1

    second = handoff()
    assert second["context"]["handoffCount"] == 2, "每次转人工都要真实计数"

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from handoffs where session_id = %s",
            (int(seeded["session_id"].replace("ses-", "")),),
        )
        assert cur.fetchone()[0] == 2


def test_handoff_requires_valid_session(client, seeded) -> None:
    resp = client.post(
        "/widget/handoff", json={"sessionId": seeded["session_id"]}
    )
    assert resp.status_code == 401


def test_empty_session_handoff_reports_no_fake_counts(client, conn) -> None:
    """D-010：空会话也能转人工；对话记录与引用依据必须是 0，不虚报。"""
    session = create_session(
        conn,
        requested_source="anonymous",
        claimed_account=None,
        host_context={"ticketNo": "WO-EMPTY"},
    )
    session_id = session["sessionId"]
    try:
        resp = client.post(
            "/widget/handoff",
            headers={SESSION_HEADER: session_id},
            json={"sessionId": session_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ticketNo"] == "WO-EMPTY"
        assert body["context"]["rounds"] == 0
        assert body["context"]["citedCount"] == 0
        assert body["context"]["handoffCount"] == 1
        assert body["hotline"]
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from sessions where id = %s", (int(session_id.split("-")[-1]),))
        conn.commit()
