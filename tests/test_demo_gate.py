"""演示站门禁与配额（需要数据库 + 假 LLM，不联网）。

钉住的边界：
- 演示码缺失 / 无效 / 停用 / 过期 → 401，且不建会话（只在前端校验等于没校验）；
- 配额三层（会话轮次 / 单 IP / 全局）各自生效；超限只发一帧 `refused/quota`，
  不写 messages、不调模型；
- 关闭演示模式时行为与本机开发完全一致（既有用例不受影响）。
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeEmbedder, requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.routers.deps import get_conn, get_embedder, get_llm, get_reranker

pytestmark = requires_db

IP_A = "203.0.113.10"
IP_B = "203.0.113.11"


class FakeLlm:
    """最小假 LLM：只吐一段字，用于确认"配额放行后才调模型"。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream_with_stats(self, messages, **kwargs):
        from modules.llm import StreamStats

        self.calls += 1
        stats = StreamStats()

        def iterator():
            stats.chunks += 1
            stats.chars += 2
            stats.first_token_ms = 1
            yield "好的"

        return iterator(), stats


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.strip().split("\n\n"):
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


def demo_settings(**overrides) -> Settings:
    base = dict(
        chat_mode="skeleton",
        demo_enabled=True,
        demo_require_code=True,
        demo_public_code="test-web",
        demo_session_turns=6,
        demo_code_daily=10,
        demo_ip_daily=10,
        demo_global_daily=10,
        demo_trust_forwarded_for=True,
    )
    base.update(overrides)
    return Settings(**base)


def make_client(conn, settings: Settings, llm: FakeLlm | None = None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm] = lambda: llm or FakeLlm()
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: None
    return TestClient(app)


@pytest.fixture()
def demo_rows(conn):
    """演示码与用量是本次用例的临时数据，跑前跑后都清掉（只清 test- 前缀与当日用量）。"""

    def wipe() -> None:
        with conn.cursor() as cur:
            cur.execute("delete from demo_usage")
            cur.execute("delete from demo_access_codes where code like 'test-%'")
        conn.commit()

    wipe()
    yield
    wipe()


def add_code(conn, code: str, *, enabled: bool = True, expires: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into demo_access_codes (code, label, enabled, expires_at) "
            "values (%s, '测试码', %s, %s) on conflict (code) do update "
            "set enabled = excluded.enabled, expires_at = excluded.expires_at",
            (code, enabled, expires),
        )
    conn.commit()


def new_session(client: TestClient, code: str, ip: str = IP_A):
    return client.post(
        "/widget/session",
        json={
            "identity": {"source": "host_injected"},
            "hostContext": {},
            "demoCode": code,
        },
        headers={"X-Forwarded-For": ip},
    )


def ask(
    client: TestClient, session_id: str, question: str, ip: str = IP_A
) -> list[tuple[str, dict]]:
    resp = client.post(
        "/chat",
        json={"sessionId": session_id, "question": question},
        headers={"X-Widget-Session": session_id, "X-Forwarded-For": ip},
    )
    assert resp.status_code == 200
    return parse_sse(resp.text)


def test_session_without_code_is_rejected(conn, demo_rows) -> None:
    client = make_client(conn, demo_settings())
    resp = client.post(
        "/widget/session",
        json={"identity": {"source": "host_injected"}, "hostContext": {}},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "DEMO_CODE_REQUIRED"


def test_invalid_disabled_and_expired_codes_are_rejected(conn, demo_rows) -> None:
    client = make_client(conn, demo_settings())
    add_code(conn, "test-disabled", enabled=False)
    add_code(conn, "test-expired", expires="2020-01-01T00:00:00Z")

    for code in ("test-missing", "test-disabled", "test-expired"):
        resp = new_session(client, code)
        assert resp.status_code == 401, code
        assert resp.json()["code"] == "DEMO_CODE_INVALID", code


def test_valid_code_creates_session_and_records_code(conn, demo_rows) -> None:
    add_code(conn, "test-valid")
    client = make_client(conn, demo_settings())
    resp = new_session(client, "test-valid")
    assert resp.status_code == 200
    session_id = resp.json()["sessionId"]

    with conn.cursor() as cur:
        cur.execute("select demo_code from sessions where id = %s", (int(session_id[4:]),))
        assert cur.fetchone()[0] == "test-valid"


def test_demo_off_keeps_old_behaviour(conn, demo_rows) -> None:
    """关闭演示模式：不需要码、不记录码（本机开发与既有用例依赖这一条）。"""
    client = make_client(conn, Settings(chat_mode="skeleton", demo_enabled=False))
    resp = new_session(client, "any-code")
    assert resp.status_code == 200
    session_id = resp.json()["sessionId"]
    with conn.cursor() as cur:
        cur.execute("select demo_code from sessions where id = %s", (int(session_id[4:]),))
        assert cur.fetchone()[0] is None


def test_session_turn_limit_blocks_before_llm(conn, demo_rows) -> None:
    add_code(conn, "test-valid")
    llm = FakeLlm()
    client = make_client(conn, demo_settings(demo_session_turns=1), llm=llm)
    session_id = new_session(client, "test-valid").json()["sessionId"]

    first = ask(client, session_id, "半月维保要检查哪些项目？")
    assert any(name == "structured" for name, _ in first)
    assert llm.calls == 1

    second = ask(client, session_id, "再问一次")
    info = dict(second)["refused"]["refused"]
    assert info["kind"] == "quota"
    assert info["reason"] == "session"
    assert llm.calls == 1, "超限后不得再调用模型"


def test_ip_daily_limit_blocks_new_sessions(conn, demo_rows) -> None:
    add_code(conn, "test-valid")
    client = make_client(
        conn, demo_settings(demo_session_turns=10, demo_ip_daily=1, demo_global_daily=10)
    )
    first_id = new_session(client, "test-valid", ip=IP_A).json()["sessionId"]
    ask(client, first_id, "第一个问题", ip=IP_A)

    second_id = new_session(client, "test-valid", ip=IP_A).json()["sessionId"]
    refused = dict(ask(client, second_id, "第二个问题", ip=IP_A))["refused"]["refused"]
    assert refused["reason"] == "ip"


def test_global_daily_limit_blocks_across_ips(conn, demo_rows) -> None:
    add_code(conn, "test-valid")
    client = make_client(
        conn, demo_settings(demo_session_turns=10, demo_ip_daily=10, demo_global_daily=1)
    )
    first_id = new_session(client, "test-valid", ip=IP_A).json()["sessionId"]
    ask(client, first_id, "第一个问题", ip=IP_A)

    second_id = new_session(client, "test-valid", ip=IP_B).json()["sessionId"]
    refused = dict(ask(client, second_id, "第二个问题", ip=IP_B))["refused"]["refused"]
    assert refused["reason"] == "global"


def test_code_daily_limit_applies_to_named_codes(conn, demo_rows) -> None:
    add_code(conn, "test-vip")
    client = make_client(
        conn,
        demo_settings(
            demo_session_turns=10, demo_ip_daily=10, demo_global_daily=10, demo_code_daily=1
        ),
    )
    first_id = new_session(client, "test-vip", ip=IP_A).json()["sessionId"]
    ask(client, first_id, "第一个问题", ip=IP_A)

    second_id = new_session(client, "test-vip", ip=IP_B).json()["sessionId"]
    refused = dict(ask(client, second_id, "第二个问题", ip=IP_B))["refused"]["refused"]
    assert refused["reason"] == "code"


def test_blocked_usage_is_recorded(conn, demo_rows) -> None:
    add_code(conn, "test-valid")
    client = make_client(conn, demo_settings(demo_session_turns=1))
    session_id = new_session(client, "test-valid").json()["sessionId"]
    ask(client, session_id, "第一个问题")
    ask(client, session_id, "第二个问题")

    with conn.cursor() as cur:
        cur.execute(
            "select blocked from demo_usage where day = current_date and scope = 'session'"
        )
        row = cur.fetchone()
    assert row is not None and row[0] >= 1


# ---------------------------------------------------------------- 后台总开关
def set_demo_switch(conn, enabled: bool) -> None:
    """直接写 app_config（等价于后台「系统配置 → 演示总开关」保存）。"""
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "insert into app_config(key, value) values ('config.demo', %s) "
            "on conflict (key) do update set value = excluded.value, updated_at = now()",
            (Jsonb({"enabled": enabled}),),
        )
    conn.commit()


def clear_demo_switch(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from app_config where key = 'config.demo'")
    conn.commit()


def test_demo_switch_pauses_new_sessions(conn, demo_rows) -> None:
    """关闭总开关：带入口码也不建会话；/widget/config 明确说 paused，而不是"没码"。"""
    add_code(conn, "test-valid")
    client = make_client(conn, demo_settings())
    set_demo_switch(conn, False)
    try:
        resp = new_session(client, "test-valid")
        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "DEMO_PAUSED"
        assert "official entry link" in resp.json()["message"]

        demo = client.get("/widget/config").json()["demo"]
        assert demo["paused"] is True
        assert demo["enabled"] is False
        assert demo["sessionTurns"] == 0
        assert "official entry link" in demo["pausedText"]
    finally:
        clear_demo_switch(conn)


def test_demo_switch_pauses_existing_session_chat(conn, demo_rows) -> None:
    """已建立的会话在暂停后也不再回答：只发一帧 refused/paused，且不调模型。"""
    add_code(conn, "test-valid")
    llm = FakeLlm()
    client = make_client(conn, demo_settings(), llm=llm)
    session_id = new_session(client, "test-valid").json()["sessionId"]

    set_demo_switch(conn, False)
    try:
        events = ask(client, session_id, "半月维保要检查哪些项目？")
        assert [name for name, _data in events] == ["refused"]
        refused = events[0][1]["refused"]
        assert refused["kind"] == "paused"
        assert "official entry link" in refused["body"]
        assert llm.calls == 0, "暂停期间不得调用模型"
    finally:
        clear_demo_switch(conn)


def test_demo_switch_only_narrows(conn, demo_rows) -> None:
    """DEMO_ENABLED=false 的部署不会被后台开关打开（只收窄，不放大）。"""
    client = make_client(conn, demo_settings(demo_enabled=False))
    set_demo_switch(conn, True)
    try:
        demo = client.get("/widget/config").json()["demo"]
        assert demo["enabled"] is False
        assert demo["paused"] is False
        resp = client.post(
            "/widget/session",
            json={"identity": {"source": "anonymous"}, "hostContext": {}},
        )
        assert resp.status_code == 200, "非演示部署照常建立会话"
    finally:
        clear_demo_switch(conn)
