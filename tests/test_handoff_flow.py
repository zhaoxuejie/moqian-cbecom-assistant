"""转人工接入编排：四类硬转、政策问句不误转、开关关闭、连续失败阈值（需要数据库）。"""

from __future__ import annotations

import json

import pytest
from conftest import FakeEmbedder, requires_db
from fastapi.testclient import TestClient

from app import messages
from app.config import Settings, get_settings
from app.main import create_app
from app.routers.deps import get_conn, get_embedder, get_llm, get_reranker
from app.sessions import create_session

pytestmark = requires_db


class _FakeLlm:
    def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
        class _Result:
            text = "I don't have verified information about that."

        return _Result()

    def stream(self, *args, **kwargs):  # noqa: ANN002, ANN003
        yield "ok"


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


@pytest.fixture()
def client(conn):
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(chat_mode="pipeline")
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: None
    app.dependency_overrides[get_llm] = lambda: _FakeLlm()
    return TestClient(app)


def _new(conn) -> str:
    return create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"country": "US", "locale": "en"},
    )["sessionId"]


def _ask(client, session_id: str, question: str):
    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session_id},
        json={"sessionId": session_id, "question": question},
    )
    assert resp.status_code == 200, resp.text
    return _events(resp.text)


@pytest.mark.parametrize(
    ("question", "category"),
    [
        ("I want a refund for my order", "refund"),
        ("I will leave a bad review on Trustpilot", "complaint"),
        ("My lawyer will file a lawsuit", "legal"),
        ("My account was hacked and there is an unauthorized charge", "account"),
    ],
)
def test_four_categories_handoff(client, conn, question: str, category: str) -> None:
    events = _ask(client, _new(conn), question)
    assert [name for name, _ in events] == ["structured", "handoff", "done"]
    payload = events[1][1]
    assert payload["category"] == category
    assert payload["rule"] == "keyword"
    assert payload["hotline"]


def test_policy_question_does_not_handoff(client, conn) -> None:
    events = _ask(client, _new(conn), "How long is the US return window?")
    assert "handoff" not in [name for name, _ in events]


def test_disabled_category_is_released(client, conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into app_config (key, value) values ('config.handoff', %s::jsonb) "
            "on conflict (key) do update set value = excluded.value",
            (json.dumps({"enabled": {"refund": False}}),),
        )
    conn.commit()
    try:
        events = _ask(client, _new(conn), "I want a refund")
        assert "handoff" not in [name for name, _ in events]
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from app_config where key = 'config.handoff'")
        conn.commit()


def test_two_failed_turns_then_handoff(client, conn) -> None:
    session_id = _new(conn)
    # 模拟最近两轮都是拒答（真实路径由 run_turn 的 gap 落库）
    messages.log_assistant_message(
        conn, session_id=session_id, content="gap 1", refused=True, gate_reason="gap"
    )
    messages.log_assistant_message(
        conn, session_id=session_id, content="gap 2", refused=True, gate_reason="gap"
    )

    events = _ask(client, session_id, "Tell me something about your hats")
    assert [name for name, _ in events] == ["structured", "handoff", "done"]
    payload = events[1][1]
    assert payload["rule"] == "consecutive_failures"
    assert "twice in a row" in payload["text"]
