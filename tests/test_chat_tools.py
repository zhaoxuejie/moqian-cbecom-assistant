"""工具调用与失败降级（需要数据库；不联网、不调真模型）。

验证三条硬规则：
1. 订单/物流问题走工具，**不调模型**（事实只来自返回值）；
2. 单号不存在 → 固定话术、不转人工；
3. 超时 → 固定降级话术 + 转人工，事件里**没有 steps**。
"""

from __future__ import annotations

import json

from conftest import FakeEmbedder, requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.routers.deps import get_conn, get_embedder, get_reranker
from app.sessions import create_session

pytestmark = requires_db


def _events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        if name:
            events.append((name, data))
    return events


def test_order_lookup_answers_from_tool_without_llm(conn) -> None:
    session = create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"country": "US", "locale": "en"},
    )
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(chat_mode="pipeline")
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: None
    client = TestClient(app)

    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session["sessionId"]},
        json={"sessionId": session["sessionId"], "question": "Where is my order WB-10001?"},
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)
    names = [name for name, _ in events]
    assert names == ["structured", "tool", "steps", "citations", "done"], names

    tool = events[1][1]
    assert tool["name"] == "query_order"
    assert tool["status"] == "ok"
    assert tool["args"] == {"order_no": "WB-10001"}

    steps = [payload for name, payload in events if name == "steps"]
    text = " ".join(step["text"] for step in steps)
    assert "WB-10001" in text and "shipped" in text
    assert all(step["citations"] == [] for step in steps), "工具事实没有知识库引用"

    done = events[-1][1]
    assert done["citationsCount"] == 0

    # 留痕：tool_calls 落了一条
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from tool_calls where session_id = %s",
            (int(session["sessionId"].split("-")[-1]),),
        )
        assert int(cur.fetchone()[0]) == 1


def test_unknown_order_number_is_fixed_copy(conn) -> None:
    session = create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"country": "US", "locale": "en"},
    )
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(chat_mode="pipeline")
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: None
    client = TestClient(app)

    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session["sessionId"]},
        json={"sessionId": session["sessionId"], "question": "Where is my order WB-99999?"},
    )
    events = _events(resp.text)
    tool = next(payload for name, payload in events if name == "tool")
    assert tool["status"] == "not_found"
    steps = [payload for name, payload in events if name == "steps"]
    assert "couldn't find" in " ".join(step["text"] for step in steps)
    assert "degraded" not in [name for name, _ in events]


def test_tool_timeout_degrades_without_steps(conn) -> None:
    session = create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"country": "US", "locale": "en"},
    )
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(
        chat_mode="pipeline", tool_timeout_seconds=0.2, demo_fault_enabled=True
    )
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: None
    client = TestClient(app)

    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session["sessionId"], "X-Demo-Fault": "tool_timeout"},
        json={"sessionId": session["sessionId"], "question": "Where is my order WB-10001?"},
    )
    events = _events(resp.text)
    names = [name for name, _ in events]
    assert names == ["structured", "tool", "degraded", "done"], names
    tool = events[1][1]
    assert tool["status"] == "timeout"
    degraded = events[2][1]
    assert degraded["handoffRequired"] is True
    assert degraded["reason"] == "timeout"
    assert "can't check your order right now" in degraded["text"]
    assert "steps" not in names, "降级时不得回半成品答案"


def test_demo_fault_is_ignored_when_disabled(conn) -> None:
    """生产关闭 DEMO_FAULT_ENABLED 后，伪造的 X-Demo-Fault 不生效。"""
    session = create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"country": "US", "locale": "en"},
    )
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(
        chat_mode="pipeline", tool_timeout_seconds=0.5, demo_fault_enabled=False
    )
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: None
    client = TestClient(app)

    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session["sessionId"], "X-Demo-Fault": "tool_timeout"},
        json={"sessionId": session["sessionId"], "question": "Where is my order WB-10001?"},
    )
    tool = next(payload for name, payload in _events(resp.text) if name == "tool")
    assert tool["status"] == "ok"
