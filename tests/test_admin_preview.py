"""知识自测接口测试（需要数据库）。

钉住三条口径：
- 安全红线在检索之前拦截，不返回任何命中；
- 抽测只返回结构化/证据门/命中证据，不生成答案；
- 未登录不可调用（治理域接口）。
"""

from __future__ import annotations

import shutil

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.ingest import parse_document, upsert_document
from app.main import create_app
from app.routers.deps import get_conn, get_embedder, get_reranker
from app.security import create_admin_token, hash_password
from app.users import create_user, get_user_by_account

pytestmark = requires_db

ACCOUNT = "T-PREVIEW-ADMIN"
PASSWORD = "Unit-Test-Pass-3"
JWT_SECRET = "unit-test-secret-32-bytes-long!!"


@pytest.fixture()
def seeded(conn, tmp_path, sample_pdf, fake_embedder):
    user = get_user_by_account(conn, ACCOUNT)
    if user is None:
        user_id = create_user(
            conn,
            account=ACCOUNT,
            name="抽测测试管理员",
            role="admin",
            password_hash=hash_password(PASSWORD),
        )
    else:
        user_id = int(user["id"])
    docs_dir = tmp_path / "original-docs"
    docs_dir.mkdir(exist_ok=True)
    shutil.copyfile(sample_pdf, docs_dir / "manual.pdf")
    settings = Settings(
        data_dir=str(tmp_path),
        doc_ingest_chunk_max_chars=200,
        doc_ingest_chunk_min_chars=20,
        jwt_secret=JWT_SECRET,
    )
    document_id, _ = upsert_document(
        conn,
        title="抽测测试手册",
        file_path="original-docs/manual.pdf",
        source_type="pdf",
        sha256="preview-test-sha256",
        meta={"category": "manual"},
        size_bytes=sample_pdf.stat().st_size,
    )
    parse_document(conn, document_id=document_id, settings=settings, embedder=fake_embedder)

    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_embedder] = lambda: fake_embedder
    app.dependency_overrides[get_reranker] = lambda: None
    token, _ = create_admin_token(
        int(user_id), ACCOUNT, "admin", Settings(jwt_secret=JWT_SECRET)
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client
    with conn.cursor() as cur:
        cur.execute("delete from documents where id = %s", (document_id,))
        cur.execute("delete from users where account = %s", (ACCOUNT,))
    conn.commit()


def test_preview_safety_blocks_before_retrieval(seeded) -> None:
    resp = seeded.post(
        "/admin/preview/ask", json={"question": "I want a refund and I will sue you"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "handoff"
    assert body["hits"] == []
    assert body["handoff"]["category"] in {"refund", "legal"}


def test_preview_returns_structured_and_gate(seeded) -> None:
    resp = seeded.post(
        "/admin/preview/ask",
        json={"question": "How long is the US return window?", "country": "US", "locale": "en"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] in {"answer", "gap"}
    assert body["structured"]["intent"] == "policy"
    assert body["structured"]["country"] == "US"
    assert isinstance(body["hits"], list)
    assert body["gate"]["passed"] is (body["kind"] == "answer")


def test_preview_requires_admin_login(seeded) -> None:
    seeded.headers.pop("Authorization", None)
    resp = seeded.post("/admin/preview/ask", json={"question": "制动器间隙标准值是多少？"})
    assert resp.status_code == 401
