"""后台治理操作接口测试（D-011，需要数据库）。

覆盖语义：
- 停用 = lifecycle=disabled：仍在列表，切片保留、可重新启用；
- 归档 = lifecycle=archived：从默认列表移除，切片保留、可恢复（D-032，与停用分开）；
- 版本处置：superseded / disabled；
- 切片启停用与出处元数据修改；正文编辑明确拒绝（需重算向量）；
- 任务取消：queued 立即 cancelled；running 置 cancelRequested，worker 收尾为 cancelled；
- 所有写操作落审计表。
"""

from __future__ import annotations

import shutil

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.ingest import parse_document, upsert_document
from app.main import create_app
from app.routers.deps import get_conn
from app.security import create_admin_token, hash_password
from app.users import create_user, get_user_by_account
from modules.tasks import enqueue
from modules.tasks import queue as task_queue

pytestmark = requires_db

ACCOUNT = "T-OPS-ADMIN"
PASSWORD = "Unit-Test-Pass-Ops"
JWT_SECRET = "unit-test-secret-32-bytes-long!!"
TEST_TITLE = "治理操作测试手册"


@pytest.fixture()
def seeded(conn, tmp_path, sample_pdf, fake_embedder):
    user = get_user_by_account(conn, ACCOUNT)
    if user is None:
        user_id = create_user(
            conn,
            account=ACCOUNT,
            name="治理操作测试管理员",
            role="admin",
            password_hash=hash_password(PASSWORD),
        )
        user = {"id": user_id}
    docs_dir = tmp_path / "original-docs"
    docs_dir.mkdir(exist_ok=True)
    shutil.copyfile(sample_pdf, docs_dir / "manual.pdf")
    settings = Settings(
        data_dir=str(tmp_path),
        doc_ingest_chunk_max_chars=200,
        doc_ingest_chunk_min_chars=20,
        jwt_secret=JWT_SECRET,
    )
    # 自愈：清掉上一次被强杀留下的同名测试文档
    with conn.cursor() as cur:
        cur.execute("delete from documents where title = %s", (TEST_TITLE,))
    conn.commit()
    document_id, _ = upsert_document(
        conn,
        title=TEST_TITLE,
        file_path="original-docs/manual.pdf",
        source_type="pdf",
        sha256="ops-test-sha256",
        meta={"category": "manual", "model": "BK系列", "version": "Ver.3"},
    )
    parse_document(conn, document_id=document_id, settings=settings, embedder=fake_embedder)

    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    token, _ = create_admin_token(
        int(user["id"]), ACCOUNT, "admin", Settings(jwt_secret=JWT_SECRET)
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield {"client": client, "document_id": document_id, "conn": conn}
    with conn.cursor() as cur:
        cur.execute("delete from jobs where payload ->> 'documentId' = %s", (str(document_id),))
        cur.execute(
            "delete from admin_audit_log where target_id in (%s, %s)",
            (f"doc-{document_id}", f"document:{document_id}"),
        )
        cur.execute("delete from documents where id = %s", (document_id,))
        # 按标题兜底：upsert 复用旧 id 时也不会留下同名残留
        cur.execute("delete from documents where title = %s", (TEST_TITLE,))
    conn.commit()


def test_disable_then_enable_is_soft_delete(seeded) -> None:
    client = seeded["client"]
    conn = seeded["conn"]
    doc_id = f"doc-{seeded['document_id']}"

    resp = client.post(f"/admin/documents/{doc_id}/enable", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["lifecycle"] == "disabled"
    assert resp.json()["status"] == "parsed", "解析状态不被停用改写"

    # 切片保留（软删除）：证据单元仍在库里
    with conn.cursor() as cur:
        cur.execute("select count(*) from chunks where document_id = %s", (seeded["document_id"],))
        assert cur.fetchone()[0] >= 1

    resp = client.post(f"/admin/documents/{doc_id}/enable", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["lifecycle"] == "current"

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from admin_audit_log where action = 'document.disable' "
            "and target_id = %s",
            (doc_id,),
        )
        assert cur.fetchone()[0] == 1


def test_archive_hides_from_default_list_and_keeps_chunks(seeded) -> None:
    """归档 ≠ 停用（D-032）：默认列表看不到、显式查得到、切片保留、可恢复、审计留痕。"""
    client = seeded["client"]
    conn = seeded["conn"]
    doc_id = f"doc-{seeded['document_id']}"

    resp = client.post(f"/admin/documents/{doc_id}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle"] == "archived"
    assert resp.json()["status"] == "parsed", "解析状态不被归档改写"

    default = client.get("/admin/documents", params={"pageSize": 100}).json()
    assert doc_id not in [item["id"] for item in default["items"]], "归档默认不进列表"
    archived = client.get(
        "/admin/documents", params={"lifecycle": "archived", "pageSize": 100}
    ).json()
    assert doc_id in [item["id"] for item in archived["items"]]

    # 切片保留：归档不是物理删除（证据单元是已发出答案的引用目标）
    with conn.cursor() as cur:
        cur.execute("select count(*) from chunks where document_id = %s", (seeded["document_id"],))
        assert cur.fetchone()[0] >= 1

    resp = client.post(f"/admin/documents/{doc_id}/restore")
    assert resp.status_code == 200
    assert resp.json()["lifecycle"] == "current"
    back = client.get("/admin/documents", params={"pageSize": 100}).json()
    assert doc_id in [item["id"] for item in back["items"]], "恢复后回到默认列表"

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from admin_audit_log where action = 'document.archive' "
            "and target_id = %s",
            (doc_id,),
        )
        assert cur.fetchone()[0] == 1, "归档要留痕，且不复用 document.disable"


def test_disable_keeps_document_in_default_list(seeded) -> None:
    """停用仍留在默认列表（只是不进检索池）；会从列表消失的只有归档。"""
    client = seeded["client"]
    doc_id = f"doc-{seeded['document_id']}"

    resp = client.post(f"/admin/documents/{doc_id}/enable", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["lifecycle"] == "disabled"

    listing = client.get("/admin/documents", params={"pageSize": 100}).json()
    assert doc_id in [item["id"] for item in listing["items"]]


def test_patch_document_metadata_syncs_chunks(seeded) -> None:
    client = seeded["client"]
    conn = seeded["conn"]
    doc_id = f"doc-{seeded['document_id']}"

    resp = client.patch(
        f"/admin/documents/{doc_id}",
        json={"title": "改过的标题", "batch": "B23", "version": "Ver.4"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "改过的标题"
    assert body["batch"] == "B23"
    assert body["version"] == "Ver.4"

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from chunks where document_id = %s and batch = 'B23' "
            "and manual_version = 'Ver.4'",
            (seeded["document_id"],),
        )
        assert cur.fetchone()[0] >= 1

    empty = client.patch(f"/admin/documents/{doc_id}", json={})
    assert empty.status_code == 422

    bad_lifecycle = client.patch(f"/admin/documents/{doc_id}", json={"lifecycle": "legacy"})
    assert bad_lifecycle.status_code == 422


def test_patch_chunk_status_and_section(seeded) -> None:
    client = seeded["client"]
    conn = seeded["conn"]
    with conn.cursor() as cur:
        cur.execute(
            "select id from chunks where document_id = %s order by id limit 1",
            (seeded["document_id"],),
        )
        chunk_id = int(cur.fetchone()[0])
    ch = f"ch-{chunk_id}"

    resp = client.patch(f"/admin/chunks/{ch}", json={"status": "disabled", "sectionNo": "9.9"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "disabled"
    assert body["sectionNo"] == "9.9"
    assert body["loc"] == "§9.9"

    text_edit = client.patch(f"/admin/chunks/{ch}", json={"text": "重写正文"})
    assert text_edit.status_code == 422
    assert "重算向量" in text_edit.json()["message"]

    # 前端表格的占位符不是真实值：— / 未标注章节 必须归一为 null（不能写进库）
    normalized = client.patch(
        f"/admin/chunks/{ch}",
        json={"sectionNo": "未标注章节", "version": "—"},
    )
    assert normalized.status_code == 200
    assert normalized.json()["sectionNo"] is None
    assert normalized.json()["version"] is None
    assert normalized.json()["loc"] == "未标注章节"

    # 行号出处（FAQ 工作表）不是章节号：保存后不能补「§」，否则前端「工作表行号」
    # 会显示成 §FAQ 第2行（页面上的多余符号就是这个 §）。
    with conn.cursor() as cur:
        cur.execute(
            "insert into chunks (document_id, evidence_id, content, page_from, page_to, meta_json) "
            "values (%s, %s, %s, 0, 0, %s) returning id",
            (
                seeded["document_id"],
                "FAQ-ROW-TEST",
                "问题：间隙是多少？\n答案：按手册。",
                Jsonb({"loc": "FAQ 第2行"}),
            ),
        )
        row_chunk_id = int(cur.fetchone()[0])
    conn.commit()
    row_resp = client.patch(f"/admin/chunks/ch-{row_chunk_id}", json={"sectionNo": "FAQ 第2行"})
    assert row_resp.status_code == 200
    assert row_resp.json()["loc"] == "FAQ 第2行"
    assert row_resp.json()["provKind"] == "工作表行号"


def test_patch_document_batch_does_not_override_row_batch(seeded) -> None:
    """文档级改批次只填「行内没标过批次」的切片，FAQ 行级批次不被覆盖。"""
    client = seeded["client"]
    conn = seeded["conn"]
    document_id = seeded["document_id"]
    with conn.cursor() as cur:
        cur.execute(
            "insert into chunks "
            "(document_id, evidence_id, content, page_from, page_to, batch, meta_json) "
            "values (%s, %s, %s, 0, 0, %s, %s) returning id",
            (
                document_id,
                "FAQ-ROW-BATCH",
                "行级批次切片",
                "B22",
                Jsonb({"loc": "FAQ 第2行", "batchFromRow": True}),
            ),
        )
        row_chunk_id = int(cur.fetchone()[0])
    conn.commit()

    resp = client.patch(f"/admin/documents/doc-{document_id}", json={"batch": "B99"})
    assert resp.status_code == 200

    with conn.cursor() as cur:
        cur.execute("select batch from chunks where id = %s", (row_chunk_id,))
        assert cur.fetchone()[0] == "B22", "行级批次不能被文档级批次覆盖"
        cur.execute(
            "select count(*) from chunks where document_id = %s and batch = 'B99' "
            "and coalesce(meta_json ->> 'batchFromRow', 'false') <> 'true'",
            (document_id,),
        )
        assert int(cur.fetchone()[0]) > 0, "没有行级批次的切片应继承文档级批次"


def test_job_cancel_queued_and_running(seeded) -> None:
    client = seeded["client"]
    conn = seeded["conn"]

    queued_id = enqueue(conn, "parse", {"documentId": str(seeded["document_id"])})
    resp = client.patch(f"/admin/jobs/job-{queued_id}", json={"action": "cancel"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["stage"] == "已取消"

    again = client.patch(f"/admin/jobs/job-{queued_id}", json={"action": "cancel"})
    assert again.status_code == 409
    retry = client.post(f"/admin/jobs/job-{queued_id}/retry")
    assert retry.status_code == 409, "已取消任务不能按失败重试"

    running_id = enqueue(conn, "parse", {"documentId": str(seeded["document_id"])})
    with conn.cursor() as cur:
        cur.execute("update jobs set status = 'running' where id = %s", (running_id,))
    conn.commit()
    resp = client.patch(f"/admin/jobs/job-{running_id}", json={"action": "cancel"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert resp.json()["cancelRequested"] is True

    # worker 收尾：取消请求优先于 done
    assert task_queue.mark_done(conn, running_id) == "cancelled"


def test_job_pause_resume_not_open(seeded) -> None:
    client = seeded["client"]
    conn = seeded["conn"]
    job_id = enqueue(conn, "parse", {"documentId": str(seeded["document_id"])})
    for action in ("pause", "resume"):
        resp = client.patch(f"/admin/jobs/job-{job_id}", json={"action": action})
        assert resp.status_code == 409
        assert "未开放" in resp.json()["message"]


def test_reparse_enqueues_and_rejects_duplicate(seeded) -> None:
    client = seeded["client"]
    doc_id = f"doc-{seeded['document_id']}"
    first = client.post(f"/admin/documents/{doc_id}/reparse")
    assert first.status_code == 202
    job = first.json()
    assert job["status"] == "queued"
    assert job["docId"] == doc_id

    second = client.post(f"/admin/documents/{doc_id}/reparse")
    assert second.status_code == 409


def test_versions_lists_self(seeded) -> None:
    client = seeded["client"]
    doc_id = f"doc-{seeded['document_id']}"
    resp = client.get(f"/admin/documents/{doc_id}/versions")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(item["id"] == doc_id for item in items)


def test_unknown_targets_are_404(seeded) -> None:
    client = seeded["client"]
    assert (
        client.post(
            "/admin/documents/doc-999999/enable", json={"enabled": False}
        ).status_code
        == 404
    )
    assert client.patch("/admin/documents/doc-999999", json={"title": "x"}).status_code == 404
    assert client.patch("/admin/chunks/ch-999999", json={"status": "disabled"}).status_code == 404
    assert client.patch("/admin/jobs/job-999999", json={"action": "cancel"}).status_code == 404
    assert client.post("/admin/documents/doc-999999/reparse").status_code == 404
