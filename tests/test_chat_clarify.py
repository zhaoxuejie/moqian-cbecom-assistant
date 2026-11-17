"""单轮追问（跨境版）：订单/物流类问题缺单号时追问，一轮为限。"""

from __future__ import annotations

import json

import pytest
from conftest import FakeEmbedder, requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.rag_runner import build_order_ask, build_structured, run_turn
from app.routers.deps import get_conn, get_embedder, get_reranker
from app.schemas import ClarifySelection, HostContext
from app.sessions import create_session

pytestmark = requires_db

ORDER_QUESTION = "Where is my order?"
ORDER_NO = "WB-10007"


def _context(country: str | None = "US") -> HostContext:
    return HostContext(country=country, locale="en")


def test_order_question_asks_for_order_number() -> None:
    structured = build_structured(ORDER_QUESTION, context=_context())
    assert structured["intent"] == "order"
    assert structured["orderNo"] is None
    assert structured["missing"] == ["order_no"]

    ask = build_order_ask(structured, None)
    assert ask is not None
    assert ask["field"] == "order_no"
    assert [option["value"] for option in ask["options"]] == ["unknown", "skip"]


def test_order_with_number_does_not_ask() -> None:
    structured = build_structured(f"Where is my order {ORDER_NO}?", context=_context())
    assert structured["orderNo"] == ORDER_NO
    assert structured["missing"] == []
    assert build_order_ask(structured, None) is None


def test_policy_question_does_not_ask() -> None:
    structured = build_structured("How long is the US return window?", context=_context())
    assert structured["intent"] == "policy"
    assert build_order_ask(structured, None) is None


def test_clarify_unknown_keeps_order_empty() -> None:
    structured = build_structured(
        ORDER_QUESTION,
        context=_context(),
        clarify=ClarifySelection(field="order_no", value="unknown"),
    )
    assert structured["orderNo"] is None
    # 追问过的请求不再触发 ask（一轮为限在 run_turn 里由 clarify 非空保证）
    assert build_order_ask(structured, ClarifySelection(field="order_no", value="unknown")) is None


def test_clarify_value_sets_order_number() -> None:
    structured = build_structured(
        ORDER_QUESTION,
        context=_context(),
        clarify=ClarifySelection(field="order_no", value=ORDER_NO.lower()),
    )
    assert structured["orderNo"] == ORDER_NO


def test_run_turn_asks_without_calling_model(conn) -> None:
    outcome = run_turn(
        settings=Settings(),
        conn=conn,
        llm=None,
        embedder=None,
        question=ORDER_QUESTION,
        context=_context(),
    )
    assert outcome.ask is not None
    assert outcome.ask["field"] == "order_no"
    assert outcome.gate_reason == "ask"


@pytest.fixture()
def chat_client(conn):
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
    try:
        yield client, session["sessionId"]
    finally:
        session_pk = int(session["sessionId"].split("-")[-1])
        with conn.cursor() as cur:
            cur.execute("delete from sessions where id = %s", (session_pk,))
        conn.commit()


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


def test_chat_emits_ask_event_before_retrieval(chat_client) -> None:
    client, session_id = chat_client
    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session_id},
        json={"sessionId": session_id, "question": ORDER_QUESTION},
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)
    assert [name for name, _ in events] == ["structured", "ask"]
    ask = events[1][1]["ask"]
    assert ask["field"] == "order_no"
    assert len(ask["options"]) >= 2


def test_chat_high_risk_short_circuits_before_ask(chat_client) -> None:
    """退款诉求优先于"缺单号"：先转人工，不要先追问单号。"""
    client, session_id = chat_client
    resp = client.post(
        "/chat",
        headers={"X-Widget-Session": session_id},
        json={"sessionId": session_id, "question": "I want a refund for my order"},
    )
    assert resp.status_code == 200, resp.text
    assert [name for name, _ in _events(resp.text)] == ["structured", "handoff", "done"]
