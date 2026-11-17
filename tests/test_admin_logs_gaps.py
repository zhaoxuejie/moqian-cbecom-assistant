"""后台「问答日志 + 知识缺口」接口测试（需要数据库）。

钉住四条最容易漂的口径：
- 一轮 = user 消息 + 相邻 assistant 消息，outcome 由拒答/证据门原因推导；
- `withStats` 的统计排除 `channel=admin_preview`，列表本身仍返回自测记录；
- 缺口是拒答日志的派生视图：聚合快照幂等回填，人工状态不被回填覆盖；
- 错误体是项目统一的 `{code, message}`。

用例自建会话/消息/缺口，结束后清理自己的记录（不动演示数据）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.main import create_app
from app.routers.deps import get_conn
from app.security import create_admin_token, hash_password
from app.users import create_user, get_user_by_account

pytestmark = requires_db

ADMIN_ACCOUNT = "T-P1-LOGS-ADMIN"
ADMIN_NAME = "P1 日志测试管理员"
ADMIN_PASSWORD = "Unit-Test-Pass-P1"
ENGINEER_ACCOUNT = "T-P1-LOGS-ENG"
ENGINEER_NAME = "P1 测试工程师"
JWT_SECRET = "unit-test-secret-32-bytes-long!!"

TAG = "P1LOG"
Q1 = f"{TAG} 主机轴承异响怎么定位"
Q2 = f"{TAG} 控制柜报 E07，先查哪几项？"
Q3 = f"{TAG} 门锁回路短接会怎样"
Q4 = f"{TAG} 带电检修安全回路"
Q5 = f"{TAG} 管理自测问题"
Q6 = Q2  # 同义重复提问 → 缺口 count 应累加

CITATION = {
    "evidenceId": "M24002",
    "kind": "manual",
    "title": "《维修手册》§1",
    "loc": "§1",
    "batch": "B23",
    "score": 0.87,
}


def _insert_session(
    conn,
    *,
    auth_source: str,
    claimed: str | None = None,
    engineer_id: int | None = None,
    channel: str = "widget",
    host_context: dict[str, Any] | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into sessions "
            "(auth_source, claimed_engineer_id, engineer_id, channel, host_context) "
            "values (%s, %s, %s, %s, %s::jsonb) returning id",
            (auth_source, claimed, engineer_id, channel, Jsonb(host_context or {})),
        )
        return int(cur.fetchone()[0])


def _insert_user(conn, session_id: int, question: str, structured: dict | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into messages (session_id, role, content, structured_query, question_at) "
            "values (%s, 'user', %s, %s::jsonb, clock_timestamp()) returning id",
            (session_id, question, Jsonb(structured) if structured else None),
        )
        return int(cur.fetchone()[0])


def _insert_assistant(
    conn,
    session_id: int,
    content: str,
    *,
    refused: bool = False,
    gate_reason: str | None = None,
    evidence_ids: list[str] | None = None,
    citations: list[dict] | None = None,
    resolved: bool | None = None,
    feedback: str | None = None,
    resolved_after_question: int | None = None,
    question_id: int | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into messages "
            "(session_id, role, content, refused, gate_reason, evidence_ids, citations, "
            " resolved, feedback, created_at) "
            "values (%s, 'assistant', %s, %s, %s, %s, %s::jsonb, %s, %s, clock_timestamp()) "
            "returning id",
            (
                session_id,
                content,
                refused,
                gate_reason,
                evidence_ids or [],
                Jsonb(citations or []),
                resolved,
                feedback,
            ),
        )
        answer_id = int(cur.fetchone()[0])
        if resolved_after_question is not None and question_id is not None:
            cur.execute(
                "update messages set resolved_at = "
                "(select question_at from messages where id = %s) + make_interval(secs => %s) "
                "where id = %s",
                (question_id, resolved_after_question, answer_id),
            )
        return answer_id


@pytest.fixture()
def seeded(conn):
    """建一个管理员 + 三个会话（已识别 / 未识别 / 管理自测）与 4 种结果轮次。"""
    admin = get_user_by_account(conn, ADMIN_ACCOUNT)
    admin_id = (
        int(admin["id"])
        if admin
        else create_user(
            conn,
            account=ADMIN_ACCOUNT,
            name=ADMIN_NAME,
            role="admin",
            password_hash=hash_password(ADMIN_PASSWORD),
        )
    )
    engineer = get_user_by_account(conn, ENGINEER_ACCOUNT)
    engineer_id = (
        int(engineer["id"])
        if engineer
        else create_user(
            conn,
            account=ENGINEER_ACCOUNT,
            name=ENGINEER_NAME,
            role="user",
            password_hash=hash_password(ADMIN_PASSWORD),
        )
    )

    with conn.cursor() as cur:
        cur.execute("select symptom_key from gaps")
        gaps_before = {row[0] for row in cur.fetchall()}
        cur.execute("select coalesce(max(id), 0) from admin_audit_log")
        audit_before = int(cur.fetchone()[0])

    session_a = _insert_session(
        conn,
        auth_source="host_injected",
        claimed=ENGINEER_ACCOUNT,
        engineer_id=engineer_id,
        host_context={"ticketNo": "T-P1-0001", "deviceModel": "BK系列", "batch": "B23"},
    )
    q1 = _insert_user(
        conn,
        session_a,
        Q1,
        {
            "symptom": Q1,
            "model": "BK系列",
            "batch": "B23",
            "code": None,
            "missing": [],
            "batchOrigin": "host_context",
        },
    )
    a1 = _insert_assistant(
        conn,
        session_a,
        "按 §1 检查主机承重梁与导向轮，引用已校验。",
        evidence_ids=["M24002"],
        citations=[CITATION],
        resolved=True,
        feedback="resolved",
        resolved_after_question=42,
        question_id=q1,
    )
    q2 = _insert_user(
        conn,
        session_a,
        Q2,
        {
            "symptom": Q2,
            "model": "BK系列",
            "batch": None,
            "code": "E07",
            "missing": ["batch"],
            "batchOrigin": None,
        },
    )
    a2 = _insert_assistant(
        conn,
        session_a,
        "未查到可靠依据",
        refused=True,
        gate_reason="gap",
    )
    with conn.cursor() as cur:
        cur.execute(
            "insert into handoffs (session_id, message_id) values (%s, %s)",
            (session_a, a2),
        )

    session_b = _insert_session(
        conn, auth_source="anonymous", claimed="T-P1-E9999", channel="widget"
    )
    q3 = _insert_user(conn, session_b, Q3, None)
    _insert_assistant(
        conn,
        session_b,
        "回答中的引用未通过校验，已整条剥离。",
        refused=True,
        gate_reason="citation_stripped",
        evidence_ids=["M24002"],
    )
    q4 = _insert_user(
        conn,
        session_b,
        Q4,
        {"symptom": Q4, "model": None, "batch": None, "code": None, "missing": ["model", "batch"]},
    )
    _insert_assistant(conn, session_b, "安全提示：请联系厂家", refused=True, gate_reason="safe")

    q6 = _insert_user(
        conn,
        session_b,
        Q6,
        {
            "symptom": Q6,
            "model": "BK系列",
            "batch": None,
            "code": "E07",
            "missing": ["batch"],
            "batchOrigin": None,
        },
    )
    _insert_assistant(conn, session_b, "未查到可靠依据", refused=True, gate_reason="gap")

    session_preview = _insert_session(
        conn, auth_source="password_login", channel="admin_preview"
    )
    q5 = _insert_user(
        conn,
        session_preview,
        Q5,
        {"symptom": Q5, "model": None, "batch": None, "code": None, "missing": []},
    )
    _insert_assistant(conn, session_preview, "自测回答", citations=[])

    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(jwt_secret=JWT_SECRET)
    token, _ = create_admin_token(
        admin_id, ADMIN_ACCOUNT, "admin", Settings(jwt_secret=JWT_SECRET)
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})

    data = {
        "admin_id": admin_id,
        "engineer_id": engineer_id,
        "session_a": session_a,
        "session_b": session_b,
        "session_preview": session_preview,
        "q1": q1,
        "a1": a1,
        "q2": q2,
        "q3": q3,
        "q4": q4,
        "q5": q5,
        "q6": q6,
    }
    try:
        yield client, data
    finally:
        sessions = [session_a, session_b, session_preview]
        with conn.cursor() as cur:
            cur.execute("delete from sessions where id = any(%s)", (sessions,))
            # 回填机会把历史拒答也聚合进 gaps；只保留用例开始前的快照，避免污染演示库
            cur.execute(
                "delete from gaps where not (symptom_key = any(%s))", (list(gaps_before),)
            )
            cur.execute("delete from admin_audit_log where id > %s", (audit_before,))
            cur.execute(
                "delete from users where account in (%s, %s)",
                (ADMIN_ACCOUNT, ENGINEER_ACCOUNT),
            )
        conn.commit()


def _page(client: TestClient, **params: Any) -> dict[str, Any]:
    resp = client.get("/admin/messages", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _find(items: list[dict], message_id: str) -> dict:
    return next(item for item in items if item["id"] == message_id)


def test_list_rounds_contract_shape(seeded) -> None:
    client, data = seeded
    body = _page(client, pageSize=50, withStats="true")
    items = body["items"]

    row = _find(items, f"msg-{data['q1']}")
    assert row["sessionId"] == f"ses-{data['session_a']}"
    assert row["user"] == ENGINEER_NAME
    assert row["identity"] == {
        "source": "host_injected",
        "claimed": ENGINEER_ACCOUNT,
        "engineer": ENGINEER_ACCOUNT,
    }
    assert row["ticket"] == "T-P1-0001"
    assert row["question"] == Q1
    assert row["structured"]["model"] == "BK系列"
    assert row["structured"]["batch"] == "B23"
    assert row["structured"]["missing"] == []
    assert row["outcome"] == "answered"
    assert row["citations"] == 1
    assert row["hits"] == [
        {"evidenceId": "M24002", "loc": "§1", "kind": "manual", "score": 0.87}
    ]
    assert row["resolved"] == "resolved"
    assert row["resolvedInSec"] == 42
    assert row["handoff"] is True
    assert row["channel"] == "widget"
    assert row["stripped"] == []

    gap_row = _find(items, f"msg-{data['q2']}")
    assert gap_row["outcome"] == "gap"
    assert gap_row["resolved"] is None
    assert gap_row["citations"] == 0

    anonymous_row = _find(items, f"msg-{data['q3']}")
    assert anonymous_row["user"] == "未识别"
    assert anonymous_row["identity"] == {
        "source": "anonymous",
        "claimed": "T-P1-E9999",
        "engineer": None,
    }
    assert anonymous_row["outcome"] == "gap"
    # 未落逐条证据 ID：只给"整条剥离"的说明项，不伪造 evidenceId
    stripped = anonymous_row["stripped"]
    assert len(stripped) == 1
    assert "剥离" in stripped[0]["reason"]
    assert "evidenceId" not in stripped[0]

    preview_row = _find(items, f"msg-{data['q5']}")
    assert preview_row["channel"] == "admin_preview"

    assert body["pagination"]["page"] == 1
    assert body["pagination"]["pageSize"] == 50
    assert body["pagination"]["total"] >= 6

    # 统计排除 admin_preview；列表仍返回自测记录
    counted = [item for item in items if item["channel"] != "admin_preview"]
    assert body["stats"] == {
        "rounds": len(counted),
        "identifiableRounds": sum(1 for item in counted if item["identity"]["engineer"]),
    }
    assert any(item["channel"] == "admin_preview" for item in items)


def test_filters_outcome_resolved_identity(seeded) -> None:
    client, data = seeded

    gaps = _page(client, outcome="gap", pageSize=100)["items"]
    assert gaps and all(item["outcome"] == "gap" for item in gaps)
    assert f"msg-{data['q2']}" in {item["id"] for item in gaps}
    assert f"msg-{data['q1']}" not in {item["id"] for item in gaps}

    resolved = _page(client, resolved="resolved", pageSize=100)["items"]
    assert resolved and all(item["resolved"] == "resolved" for item in resolved)
    assert f"msg-{data['q1']}" in {item["id"] for item in resolved}

    none = _page(client, resolved="none", pageSize=100)["items"]
    assert none and all(item["resolved"] is None for item in none)
    assert f"msg-{data['q2']}" in {item["id"] for item in none}

    identified = _page(client, identity="host_injected", pageSize=100)["items"]
    assert identified and all(item["identity"]["source"] == "host_injected" for item in identified)
    assert f"msg-{data['q1']}" in {item["id"] for item in identified}

    unidentified = _page(client, identity="unidentified", pageSize=100)["items"]
    assert unidentified and all(item["identity"]["engineer"] is None for item in unidentified)
    assert {f"msg-{data['q3']}", f"msg-{data['q4']}"} <= {item["id"] for item in unidentified}


def test_date_filter_and_pagination(seeded) -> None:
    client, data = seeded
    today = dt.date.today()
    body = _page(client, **{"from": today.isoformat(), "to": today.isoformat()}, pageSize=100)
    assert f"msg-{data['q1']}" in {item["id"] for item in body["items"]}

    future = (today + dt.timedelta(days=1)).isoformat()
    empty = _page(client, **{"from": future}, pageSize=100)
    assert empty["pagination"]["total"] == 0
    assert empty["items"] == []

    first = _page(client, page=1, pageSize=2)
    second = _page(client, page=2, pageSize=2)
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert {item["id"] for item in first["items"]} & {
        item["id"] for item in second["items"]
    } == set()


def test_chain_rounds_and_gate_reason(seeded) -> None:
    client, data = seeded
    resp = client.get(f"/admin/messages/msg-{data['q2']}/chain")
    assert resp.status_code == 200, resp.text
    chain = resp.json()

    message = chain["message"]
    assert message["id"] == f"msg-{data['q2']}"
    assert message["outcome"] == "gap"
    assert message["structured"]["code"] == "E07"

    rounds = chain["rounds"]
    assert [row["question"] for row in rounds] == [Q1, Q2]
    assert rounds[0]["outcome"] == "answered"
    assert rounds[0]["gateReason"] is None
    assert rounds[0]["citations"] and rounds[0]["citations"][0]["evidenceId"] == "M24002"
    assert rounds[0]["elapsedMs"] >= 0
    assert rounds[0]["askFields"] == []
    assert rounds[1]["outcome"] == "gap"
    assert rounds[1]["gateReason"] == "gap"
    assert rounds[1]["askFields"] == ["batch"]
    assert rounds[1]["answerSummary"] == "未查到可靠依据"

    # 用回答 id 也能打开同一轮链路
    via_answer = client.get(f"/admin/messages/msg-{data['a1']}/chain")
    assert via_answer.status_code == 200
    assert via_answer.json()["message"]["id"] == f"msg-{data['q1']}"

    assert client.get("/admin/messages/msg-99999999/chain").status_code == 404
    assert client.get("/admin/messages/not-an-id/chain").status_code == 404


def test_chain_marks_citation_stripped(seeded) -> None:
    client, data = seeded
    chain = client.get(f"/admin/messages/msg-{data['q3']}/chain").json()
    stripped = chain["message"]["stripped"]
    assert len(stripped) == 1
    assert "剥离" in stripped[0]["reason"]
    assert "evidenceId" not in stripped[0]  # 不编造具体证据 ID
    round_row = chain["rounds"][0]
    assert round_row["outcome"] == "gap"
    assert round_row["gateReason"] == "citation_stripped"
    assert round_row["stripped"] == ["回答中的引用未通过引用校验，已整条剥离（具体证据 ID 未落库）"]


def test_gaps_aggregate_from_refusals(seeded) -> None:
    client, data = seeded
    resp = client.get("/admin/gaps", params={"withStats": "true", "pageSize": 100})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    gap = next(
        item
        for item in body["items"]
        if f"msg-{data['q2']}" in item["relatedMessages"]
    )
    assert gap["id"].startswith("gap-")
    assert gap["symptom"] == Q2
    assert gap["model"] == "BK系列"
    assert gap["count"] == 2  # Q2 与 Q6 归一化后同义
    assert gap["status"] == "open"
    assert gap["coveredBy"] is None
    assert {f"msg-{data['q2']}", f"msg-{data['q6']}"} <= set(gap["relatedMessages"])
    assert gap["firstAt"] and gap["lastAt"]

    assert body["pagination"]["total"] >= 1
    assert body["stats"]["uncovered"] >= 1
    # `/admin/gaps` 每次 GET 都会跑 `_sync_gaps` 校准快照：库里更早的历史缺口若被现有语料
    # 命中，status 会变成 covered（实测 gap-687「系统配置」即如此）。因此不能断言"全局
    # covered = 0"——那与语料相关，知识源一变就失败。这里改校验真正该保证的不变量：
    # stats 与列表同源（取满一页时两边必须对得上）。
    if body["pagination"]["total"] <= body["pagination"]["pageSize"]:
        covered_in_items = sum(1 for item in body["items"] if item["status"] == "covered")
        open_in_items = sum(1 for item in body["items"] if item["status"] == "open")
        assert body["stats"]["covered"] == covered_in_items
        assert body["stats"]["uncovered"] == open_in_items

    covered = client.get("/admin/gaps", params={"status": "covered", "pageSize": 100}).json()
    assert all(item["status"] == "covered" for item in covered["items"])


def test_patch_gap_status_and_covered_by(seeded) -> None:
    client, data = seeded
    body = client.get("/admin/gaps", params={"pageSize": 100}).json()
    gap = next(item for item in body["items"] if f"msg-{data['q2']}" in item["relatedMessages"])

    resp = client.patch(
        f"/admin/gaps/{gap['id']}",
        json={"status": "covered", "coveredBy": "doc-1", "note": "已补 BK 系列手册"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["status"] == "covered"
    assert updated["coveredBy"] == "doc-1"

    covered = client.get("/admin/gaps", params={"status": "covered", "pageSize": 100}).json()
    assert gap["id"] in {item["id"] for item in covered["items"]}

    reopened = client.patch(f"/admin/gaps/{gap['id']}", json={"status": "open"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["coveredBy"] is None

    # 回填不会覆盖人工维护的状态
    again = client.get("/admin/gaps", params={"pageSize": 100}).json()
    kept = next(item for item in again["items"] if item["id"] == gap["id"])
    assert kept["status"] == "open"

    missing = client.patch("/admin/gaps/gap-99999999", json={"status": "open"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"

    invalid = client.patch(f"/admin/gaps/{gap['id']}", json={"status": "bogus"})
    assert invalid.status_code == 422


def test_endpoints_require_admin_token(seeded) -> None:
    client, _ = seeded
    client.headers.pop("Authorization", None)
    for path in ("/admin/messages", "/admin/messages/msg-1/chain", "/admin/gaps"):
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.json()["code"] == "UNAUTHORIZED"
    patch = client.patch("/admin/gaps/gap-1", json={"status": "open"})
    assert patch.status_code == 401
