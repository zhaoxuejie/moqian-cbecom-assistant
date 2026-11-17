"""会话列表 / 详情 / 证据导出（需要数据库）。"""

from __future__ import annotations

import hashlib
import json

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app import export as export_module
from app import messages
from app.main import create_app
from app.routers.deps import get_conn, require_governance_admin
from app.sessions import create_session

pytestmark = requires_db


@pytest.fixture()
def admin_client(conn):
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[require_governance_admin] = lambda: {
        "account": "test-admin",
        "role": "admin",
    }
    return TestClient(app)


@pytest.fixture()
def session_with_records(conn):
    session = create_session(
        conn,
        requested_source="host_injected",
        claimed_account=None,
        host_context={"country": "US", "locale": "en", "orderNo": "WB-10001"},
    )
    sid = session["sessionId"]

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into documents (source_type, title, doc_code, manual_version,
                                   effective_date, status, meta_json)
            values ('md', 'Return Policy', 'return-policy', '2.0', '2026-03-01', 'parsed',
                    '{"category": "policy"}'::jsonb)
            returning id
            """
        )
        doc_id = int(cur.fetchone()[0])
        cur.execute(
            """
            insert into chunks (document_id, evidence_id, content, section_no, manual_version,
                                meta_json, bm25_tokens)
            values (%s, 'RP90001', %s, 'Return windows by country', '2.0',
                    '{}'::jsonb, 'return window')
            """,
            (doc_id, "United States: 30 days from delivery."),
        )
    conn.commit()

    messages.log_user_message(
        conn, session_id=sid, question="How long is the US return window?",
        structured_query={"intent": "policy", "language": "en"},
    )
    assistant_id = messages.log_assistant_message(
        conn,
        session_id=sid,
        content="1. 30 days from delivery [RP90001]",
        evidence_ids=["RP90001"],
        citations=[
            {
                "evidenceId": "RP90001",
                "kind": "manual",
                "title": "Returns & refunds policy",
                "loc": "§Return windows by country",
            }
        ],
    )
    messages.log_tool_call(
        conn,
        session_id=sid,
        message_id=assistant_id,
        tool="query_order",
        args={"order_no": "WB-10001"},
        status="ok",
        latency_ms=12,
        result={"status": "ok", "orderNo": "WB-10001"},
    )
    messages.log_handoff(
        conn,
        session_id=sid,
        message_id=assistant_id,
        context={"reason": "refund", "rule": "keyword", "hotline": "support@x"},
    )
    yield {"session_id": sid, "doc_id": doc_id}

    with conn.cursor() as cur:
        cur.execute("delete from sessions where id = %s", (int(sid.split("-")[-1]),))
        cur.execute("delete from documents where id = %s", (doc_id,))
    conn.commit()


def test_list_filters_by_country_and_handoff_reason(admin_client, session_with_records) -> None:
    resp = admin_client.get("/admin/conversations?country=US&handoffReason=refund")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [item["id"] for item in body["items"]]
    assert session_with_records["session_id"] in ids
    item = next(item for item in body["items"] if item["id"] == session_with_records["session_id"])
    assert item["orderNo"] == "WB-10001"
    assert item["handoffReason"] == "refund"
    assert item["toolCount"] == 1


def test_package_contains_evidence_and_integrity(admin_client, session_with_records) -> None:
    resp = admin_client.get(f"/admin/conversations/{session_with_records['session_id']}")
    assert resp.status_code == 200, resp.text
    package = resp.json()

    assert package["exportVersion"] == export_module.EXPORT_VERSION
    assert package["disclaimer"]["fictional"]
    assert package["disclaimer"]["notLegalAdvice"]
    assert package["session"]["country"] == "US"
    assert package["session"]["orderNo"] == "WB-10001"

    assistant = next(m for m in package["messages"] if m["role"] == "assistant")
    assert assistant["citations"][0]["evidenceId"] == "RP90001"
    assert assistant["toolCalls"][0]["tool"] == "query_order"
    assert assistant["handoff"]["context"]["reason"] == "refund"

    evidence = package["evidence"][0]
    assert evidence["evidenceId"] == "RP90001"
    assert "30 days" in evidence["contentExcerpt"]
    assert evidence["version"] == "2.0"

    # sha256 必须可复算：去掉 integrity 后按同样规则重算
    digest = package["integrity"]["sha256"]
    payload = {key: value for key, value in package.items() if key != "integrity"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == digest


def test_markdown_contains_required_sections(admin_client, session_with_records) -> None:
    resp = admin_client.get(
        f"/admin/conversations/{session_with_records['session_id']}/export?format=md"
    )
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.text
    for marker in (
        "# Customer conversation evidence package",
        "## Disclaimer",
        "## Conversation",
        "## Evidence snapshot",
        "## Documents used",
        "## Integrity",
        "RP90001",
        "query_order",
    ):
        assert marker in text, marker


def test_json_export_has_download_header(admin_client, session_with_records) -> None:
    resp = admin_client.get(
        f"/admin/conversations/{session_with_records['session_id']}/export?format=json"
    )
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.json()["integrity"]["algorithm"] == "sha256"


def test_missing_conversation_is_404(admin_client) -> None:
    assert admin_client.get("/admin/conversations/ses-99999999").status_code == 404
    assert admin_client.get("/admin/conversations/ses-99999999/export").status_code == 404
