"""后台知识源接口测试（需要数据库）：列表 / 详情 / 切片预览页码 / 任务看板。

目的不是覆盖率，而是钉住两个容易漂的口径：
- 切片预览必须能看到**页码出处**（S2 的验收点）；
- 列表与 `stats` 同源（页脚"共 N 条"与卡片数字不能两套算法）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.ingest import parse_document, upsert_document
from app.main import create_app
from app.routers.deps import get_conn
from app.security import create_admin_token, hash_password
from app.users import create_user, get_user_by_account

pytestmark = requires_db

ACCOUNT = "T-S2-ADMIN"
PASSWORD = "Unit-Test-Pass-2"
JWT_SECRET = "unit-test-secret-32-bytes-long!!"
TEST_TITLE = "S2 测试手册（接口用例）"


@pytest.fixture()
def seeded(conn, tmp_path, sample_pdf, fake_embedder):
    """建一个管理员 + 一份已解析文档（3 页、含 §6.4.2 切片）。"""
    user = get_user_by_account(conn, ACCOUNT)
    if user is None:
        user_id = create_user(
            conn,
            account=ACCOUNT,
            name="S2 测试管理员",
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
    # 自愈：上一次测试若被强杀，同名残留会污染演示库（"BK系列"就是这么混进知识源列表的）
    with conn.cursor() as cur:
        cur.execute("delete from documents where title = %s", (TEST_TITLE,))
    conn.commit()
    document_id, _ = upsert_document(
        conn,
        title=TEST_TITLE,
        file_path="original-docs/manual.pdf",
        source_type="pdf",
        sha256="api-test-sha256",
        meta={"category": "manual", "model": "BK系列", "version": "Ver.3"},
        size_bytes=sample_pdf.stat().st_size,
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
    client.app_settings = settings  # 原文预览用例需要定位 tmp 原件目录
    yield client, document_id
    with conn.cursor() as cur:
        # 按标题清理（幂等）：即使 upsert 复用了旧 id，也不会留下同名残留
        cur.execute("delete from documents where title = %s", (TEST_TITLE,))
    conn.commit()


def test_documents_list_with_stats(seeded) -> None:
    client, document_id = seeded
    resp = client.get("/admin/documents", params={"withStats": "1", "q": TEST_TITLE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 1
    item = body["items"][0]
    assert item["id"] == f"doc-{document_id}"
    assert item["category"] == "manual"
    assert item["status"] == "parsed"
    assert item["model"] == "BK系列"
    assert item["version"] == "Ver.3"
    assert item["chunks"] >= 1
    # 列表要显示原型同款文件大小（不是 "—"）
    assert item["size"] and item["size"] != "—"
    assert item["size"].split(" ")[-1] in {"B", "KB", "MB"}
    assert body["stats"]["total"] == 1
    assert body["stats"]["totalChunks"] >= item["chunks"], "全局证据单元数不少于本文档切片数"


def test_document_detail_exposes_parse_meta(seeded) -> None:
    client, document_id = seeded
    resp = client.get(f"/admin/documents/doc-{document_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["parseMeta"]["pages"] == 3
    assert body["parseMeta"]["usedToc"] is True
    assert body["filePath"] == "original-docs/manual.pdf"


def test_chunks_show_page_provenance(seeded) -> None:
    client, document_id = seeded
    resp = client.get(f"/admin/documents/doc-{document_id}/chunks")
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"]
    assert items
    for item in items:
        assert item["evidenceId"].startswith("M")
        assert item["page"] and item["page"].startswith("P"), "切片必须能看到页码出处"
        assert item["provKind"] == "所属章节 / 页码"
        assert item["status"] == "active"

    bearing = next(item for item in items if "听音杆" in item["text"])
    assert bearing["page"] == "P2"
    assert bearing["sectionNo"] == "6.4.2"
    assert bearing["loc"] == "§6.4.2"


def test_chunks_search_filter(seeded) -> None:
    client, document_id = seeded
    resp = client.get(
        f"/admin/documents/doc-{document_id}/chunks", params={"q": "听音杆"}
    )
    body = resp.json()
    assert body["pagination"]["total"] >= 1
    assert all("听音杆" in item["text"] for item in body["items"])


def test_list_requires_admin_token(seeded) -> None:
    client, _ = seeded
    client.headers.pop("Authorization", None)
    resp = client.get("/admin/documents")
    assert resp.status_code == 401


def test_unknown_document_is_404(seeded) -> None:
    client, _ = seeded
    assert client.get("/admin/documents/doc-999999").status_code == 404


def test_jobs_listing_and_stats(seeded) -> None:
    client, document_id = seeded
    # 手动插一条用于关联文档的任务（解析任务由 import/upload 产生，这里不重复跑）
    resp = client.get("/admin/jobs", params={"withStats": "1"})
    assert resp.status_code == 200
    body = resp.json()
    assert "counts" in body["stats"]
    assert set(body["stats"]["counts"]) == {
        "queued",
        "running",
        "done",
        "failed",
        "cancelled",
    }


def test_job_without_document_omits_format_and_category(seeded, conn) -> None:
    """非文档任务（自检 / 未注册类型）不得谎报格式。

    以前 `job_to_contract` 把左连接得到的空 `source_type` 兜底成 `"pdf"`、分类兜底成
    `"manual"`，界面上就出现「（未关联知识源）· PDF」这种假信息。现在这两个字段只在
    确实关联了知识源时才返回（契约里它们本来就不是 required）。
    """
    client, _ = seeded
    with conn.cursor() as cur:
        cur.execute(
            "insert into jobs (type, payload, status) values ('llm.smoke', '{}'::jsonb, 'done') "
            "returning id"
        )
        job_id = int(cur.fetchone()[0])
    conn.commit()
    try:
        body = client.get("/admin/jobs", params={"pageSize": 100}).json()
        job = next(item for item in body["items"] if item["id"] == f"job-{job_id}")
        assert job["docId"] is None
        assert job["docTitle"] == ""
        assert "format" not in job
        assert "category" not in job
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from jobs where id = %s", (job_id,))
        conn.commit()


def test_document_original_preview_pdf(seeded) -> None:
    """PDF 原件：详情声明有原件，文件接口 inline，preview 返回 pdf，下载走附件。"""
    client, document_id = seeded
    detail = client.get(f"/admin/documents/doc-{document_id}").json()
    assert detail["hasOriginal"] is True

    inline = client.get(f"/admin/documents/doc-{document_id}/file")
    assert inline.status_code == 200
    assert inline.headers["content-type"].startswith("application/pdf")
    assert "inline" in inline.headers["content-disposition"]
    assert inline.content.startswith(b"%PDF")

    attachment = client.get(
        f"/admin/documents/doc-{document_id}/file", params={"download": "1"}
    )
    assert attachment.status_code == 200
    assert "attachment" in attachment.headers["content-disposition"]

    preview = client.get(f"/admin/documents/doc-{document_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["kind"] == "pdf"


def test_document_original_preview_text(seeded, conn) -> None:
    """文本原件按后缀返回 kind=text；不冒充 PDF。"""
    client, _ = seeded
    data_dir = Path(client.app_settings.data_dir)
    (data_dir / "original-docs" / "notes.txt").write_text(
        "第一行：制动器间隙\n第二行：0.4mm", encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (source_type, title, file_path, status) "
            "values ('pdf', '文本原件测试', 'original-docs/notes.txt', 'parsed') "
            "returning id"
        )
        doc_id = int(cur.fetchone()[0])
    conn.commit()
    try:
        preview = client.get(f"/admin/documents/doc-{doc_id}/preview")
        assert preview.status_code == 200
        body = preview.json()
        assert body["kind"] == "text"
        assert "制动器间隙" in body["text"]
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def _faq_xlsx_bytes() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "FAQ"
    sheet.append(["分类", "问题", "答案", "关键词"])
    sheet.append(["间隙", "测试问题：间隙是多少？", "测试答案：以手册为准。", "间隙|测试"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    from io import BytesIO

    from docx import Document

    document = Document()
    document.add_paragraph("BK 系列制动器维护保养手册", style="Heading 1")
    document.add_paragraph("本手册适用于 BK-PM* 与 BK-FM* 系列制动器。")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_upload_markdown_and_docx_accepted(seeded, conn) -> None:
    """手册与文档分类接受 .md 与 .docx，格式按后缀落库（解析由 worker 异步做）。"""
    client, _ = seeded
    cases = [
        ("guide.md", "# 制动器维护\n\n断电挂牌后测量间隙。".encode(), "text/markdown", "md"),
        (
            "manual.docx",
            _docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
    ]
    created: list[int] = []
    try:
        for name, payload, mime, expect in cases:
            resp = client.post(
                "/admin/documents",
                files={"file": (name, payload, mime)},
                data={"category": "manual"},
            )
            assert resp.status_code == 202, resp.text
            doc_id = int(resp.json()["documentId"].split("-")[1])
            created.append(doc_id)
            detail = client.get(f"/admin/documents/doc-{doc_id}").json()
            assert detail["format"] == expect
            assert detail["category"] == "manual"
            assert detail["hasOriginal"] is True
    finally:
        with conn.cursor() as cur:
            for doc_id in created:
                cur.execute(
                    "delete from jobs where payload->>'documentId' = %s", (str(doc_id),)
                )
                cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def test_upload_persists_manual_version_and_effective_date(seeded, conn) -> None:
    """版本与生效日期是两个字段：分开发，分别落到 manual_version / effective_date。"""
    client, _ = seeded
    resp = client.post(
        "/admin/documents",
        files={"file": ("version-date.md", b"# t\n\nbody", "text/markdown")},
        data={"category": "manual", "manualVersion": "v3.2", "effectiveDate": "2025-06-01"},
    )
    assert resp.status_code == 202, resp.text
    doc_id = int(resp.json()["documentId"].split("-")[1])
    try:
        detail = client.get(f"/admin/documents/doc-{doc_id}").json()
        assert detail["version"] == "v3.2"
        assert detail["effective"] == "2025-06-01"
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from jobs where payload->>'documentId' = %s", (str(doc_id),))
            cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def test_upload_rejects_invalid_effective_date(seeded) -> None:
    """日期格式不合法直接 422，不把整串塞进版本字段。"""
    client, _ = seeded
    resp = client.post(
        "/admin/documents",
        files={"file": ("bad-date.md", b"# t\n\nbody", "text/markdown")},
        data={"category": "manual", "effectiveDate": "2025/06/01"},
    )
    assert resp.status_code == 422
    assert "YYYY-MM-DD" in resp.json()["message"]


def test_upload_rejects_legacy_doc(seeded) -> None:
    """旧版 .doc 不能静默失败：给出「另存为 .docx」的可操作提示。"""
    client, _ = seeded
    resp = client.post(
        "/admin/documents",
        files={"file": ("old.doc", b"binary", "application/msword")},
        data={"category": "manual"},
    )
    assert resp.status_code == 422
    assert ".docx" in resp.json()["message"]


def test_document_original_preview_docx(seeded, conn) -> None:
    """Word 原件按段落导出纯文本，标题带 ## 标记。"""
    client, _ = seeded
    data_dir = Path(client.app_settings.data_dir)
    (data_dir / "original-docs" / "manual-preview.docx").write_bytes(_docx_bytes())
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (source_type, title, file_path, status) "
            "values ('docx', 'Word 预览测试', 'original-docs/manual-preview.docx', 'parsed') "
            "returning id"
        )
        doc_id = int(cur.fetchone()[0])
    conn.commit()
    try:
        preview = client.get(f"/admin/documents/doc-{doc_id}/preview")
        assert preview.status_code == 200
        body = preview.json()
        assert body["kind"] == "text"
        assert body["format"] == "docx"
        assert "## BK 系列制动器维护保养手册" in body["text"]
        assert "BK-FM*" in body["text"]
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def test_document_original_preview_xlsx(seeded, conn) -> None:
    """Excel 原件按行导出纯文本（能看到表头与问题），不伪装成 PDF。"""
    client, _ = seeded
    data_dir = Path(client.app_settings.data_dir)
    (data_dir / "original-docs" / "faq-preview.xlsx").write_bytes(_faq_xlsx_bytes())
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (source_type, title, file_path, status) "
            "values ('excel', 'FAQ 预览测试', 'original-docs/faq-preview.xlsx', 'parsed') "
            "returning id"
        )
        doc_id = int(cur.fetchone()[0])
    conn.commit()
    try:
        preview = client.get(f"/admin/documents/doc-{doc_id}/preview")
        assert preview.status_code == 200
        body = preview.json()
        assert body["kind"] == "text"
        assert "工作表：FAQ" in body["text"]
        assert "问题" in body["text"] and "间隙是多少" in body["text"]
        assert body["truncated"] is False
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def test_upload_faq_xlsx_needs_faq_category(seeded) -> None:
    """FAQ Excel 必须把业务大类选成 faq：选 manual 直接 422，并给出可操作的提示。"""
    client, _ = seeded
    resp = client.post(
        "/admin/documents",
        files={
            "file": (
                "faq-wrong-cat.xlsx",
                _faq_xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"category": "manual"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "FAQ" in body["message"]


def test_upload_faq_xlsx_accepted(seeded, conn) -> None:
    """业务大类选 faq 时 .xlsx 可上传，建档分类为 faq（解析由 worker 异步做）。"""
    client, _ = seeded
    resp = client.post(
        "/admin/documents",
        files={
            "file": (
                "faq-upload-test.xlsx",
                _faq_xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"category": "faq"},
    )
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["status"] == "uploaded"
    doc_id = int(payload["documentId"].split("-")[1])
    try:
        detail = client.get(f"/admin/documents/doc-{doc_id}").json()
        assert detail["category"] == "faq"
        assert detail["format"] == "excel"
        assert detail["model"] in (None, "—"), "FAQ 不从文件名推断型号"
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from jobs where payload->>'documentId' = %s", (str(doc_id),))
            cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def _table_xlsx_bytes() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "参数表"
    sheet.append(["型号", "间隙标准(mm)", "备注"])
    sheet.append(["BK", 0.4, "两侧均匀"])
    sheet.append(["DZS", 0.3, ""])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_upload_table_xlsx_and_csv_accepted(seeded, conn) -> None:
    """「数据表格」分类接受 Excel 与 CSV：参数/规格表的常规格式，解析由 worker 异步做。"""
    client, _ = seeded
    cases = [
        ("params-upload.xlsx", _table_xlsx_bytes(),
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "excel"),
        ("params-upload.csv", "型号,间隙标准\nBK,0.4\n".encode("utf-8-sig"), "text/csv", "csv"),
    ]
    for name, content, mime, expected_format in cases:
        resp = client.post(
            "/admin/documents",
            files={"file": (name, content, mime)},
            data={"category": "table"},
        )
        assert resp.status_code == 202, resp.text
        doc_id = int(resp.json()["documentId"].split("-")[1])
        try:
            detail = client.get(f"/admin/documents/doc-{doc_id}").json()
            assert detail["category"] == "table"
            assert detail["format"] == expected_format
        finally:
            with conn.cursor() as cur:
                cur.execute("delete from jobs where payload->>'documentId' = %s", (str(doc_id),))
                cur.execute("delete from documents where id = %s", (doc_id,))
            conn.commit()


def test_upload_ticket_export_accepted(seeded, conn) -> None:
    """工单数据的 Excel/CSV 导出按「工单」分类入库（一行一条历史工单）。"""
    client, _ = seeded
    csv_bytes = "工单号,现象,处置\nT001,异响,调间隙\n".encode("utf-8-sig")
    resp = client.post(
        "/admin/documents",
        files={"file": ("tickets.csv", csv_bytes, "text/csv")},
        data={"category": "ticket"},
    )
    assert resp.status_code == 202, resp.text
    doc_id = int(resp.json()["documentId"].split("-")[1])
    try:
        detail = client.get(f"/admin/documents/doc-{doc_id}").json()
        assert detail["category"] == "ticket"
        assert detail["format"] == "csv"
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from jobs where payload->>'documentId' = %s", (str(doc_id),))
            cur.execute("delete from documents where id = %s", (doc_id,))
        conn.commit()


def test_upload_table_rejects_pdf(seeded) -> None:
    """数据表格只收 Excel / CSV：PDF 选错分类时给出可操作提示，而不是笼统报错。"""
    client, _ = seeded
    resp = client.post(
        "/admin/documents",
        files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
        data={"category": "table"},
    )
    assert resp.status_code == 422
    assert ".xlsx" in resp.json()["message"] and ".csv" in resp.json()["message"]


def test_document_without_original_and_path_escape(seeded, conn) -> None:
    """没有原件的文档 404；路径逃逸不能读出 data/original-docs 以外的文件。"""
    client, document_id = seeded
    with conn.cursor() as cur:
        cur.execute(
            "insert into documents (source_type, title, file_path, status) "
            "values ('ticket', '工单沉淀无原件', null, 'parsed') returning id"
        )
        ticket_id = int(cur.fetchone()[0])
    conn.commit()
    try:
        detail = client.get(f"/admin/documents/doc-{ticket_id}").json()
        assert detail["hasOriginal"] is False
        assert client.get(f"/admin/documents/doc-{ticket_id}/file").status_code == 404
        assert client.get(f"/admin/documents/doc-{ticket_id}/preview").status_code == 404
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from documents where id = %s", (ticket_id,))
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "update documents set file_path = '../escape.pdf' where id = %s",
            (document_id,),
        )
    conn.commit()
    try:
        assert client.get(f"/admin/documents/doc-{document_id}/file").status_code == 404
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "update documents set file_path = 'original-docs/manual.pdf' "
                "where id = %s",
                (document_id,),
            )
        conn.commit()
