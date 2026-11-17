"""入库流水线编排：解析 → 切片 → 向量化 → 落库（幂等）。

分工：
- 解析与切片是机制 → `modules/doc_ingest`；
- 证据 ID 前缀、状态机、落库事务、向量化批大小 → `app`（这里）；
- 向量调用本身 → `modules/llm.EmbeddingClient`（可注入，测试用假客户端）。

幂等性：每次解析先删掉该文档的旧切片再重建（同一文档重复解析不会翻倍），
文档 `file_sha256` 用于 import 阶段防重复建档。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml

from modules.doc_ingest import (
    chunk_document,
    is_probably_scanned,
    parse_docx,
    parse_faq_xlsx,
    parse_pdf,
    parse_plaintext,
    parse_table,
)
from modules.llm import EmbeddingClient

from .config import Settings
from .domain import DEFAULT_EVIDENCE_PREFIX, EVIDENCE_ID_PREFIX

logger = logging.getLogger("app.ingest")

_FRONT_MATTER_FENCE = "---"

# 语料 doc_id → 证据分类。分类决定证据 ID 前缀（RP/SP/SZ/PM/PR/PV/OP/FQ），
# 也决定后台知识源列表的分组。新增语料时在这里登记一行，不要靠猜标题。
_DOC_ID_CATEGORY: dict[str, str] = {
    "return-policy": "policy",
    "return-policy-v1": "policy",
    "shipping-policy": "shipping",
    "size-guide": "size",
    "payment-faq": "payment",
    "promotion-terms": "promotion",
    "privacy-gdpr": "privacy",
    "order-modification": "order",
    "product-faq": "faq",
    "brand-story": "faq",
    "support-contact": "faq",
    "b2b-wholesale": "faq",
    "refund-process": "refund",
}

_INACTIVE_STATUSES = frozenset({"archived", "expired", "superseded", "disabled"})


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown 顶部的 YAML front matter，返回 `(meta, 正文)`。

    语料约定见 `samples/demo-corpus/README.md`：
    `doc_id / title / version / effective_date / expires_on / applies_to / status`。

    没有 front matter、或 YAML 不合法时返回 `({}, 原文)`：元数据缺失不该阻断入库，
    "原件能不能解析"比"元数据全不全"更基础；缺元数据的文档按格式默认分类处理。
    """
    stripped = text.lstrip("\ufeff")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        return {}, text
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == _FRONT_MATTER_FENCE),
        None,
    )
    if end is None:
        return {}, text
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        logger.warning("front matter 解析失败，按无元数据处理：%s", exc)
        return {}, text
    if not isinstance(loaded, dict):
        return {}, text
    return loaded, "\n".join(lines[end + 1 :]).lstrip("\n")


def category_from_doc_id(doc_id: str | None) -> str | None:
    """doc_id → 证据分类（精确匹配优先，其次最长前缀匹配，匹配不到返回 None）。"""
    text = (doc_id or "").strip()
    if not text:
        return None
    if text in _DOC_ID_CATEGORY:
        return _DOC_ID_CATEGORY[text]
    for prefix in sorted(_DOC_ID_CATEGORY, key=len, reverse=True):
        if text.startswith(prefix):
            return _DOC_ID_CATEGORY[prefix]
    return None


def front_matter_metadata(front: dict[str, Any], *, filename: str) -> dict[str, Any]:
    """把 front matter 整理成 `documents.meta_json` 那份形状（键名 camelCase，与切片共用）。

    只保留有值的键：空字段不进 meta，后台列表不会出现「适用国家：—」这类噪声。
    """
    doc_id = str(front.get("doc_id") or "").strip() or None
    category = category_from_doc_id(doc_id) or ("faq" if front.get("title") else "manual")
    raw_applies = front.get("applies_to") or []
    if isinstance(raw_applies, str):
        raw_applies = [part.strip() for part in raw_applies.split(",") if part.strip()]
    meta: dict[str, Any] = {
        "category": category,
        "docId": doc_id,
        "version": str(front["version"]) if front.get("version") is not None else None,
        "status": str(front.get("status") or "current"),
        "appliesTo": [str(item).strip() for item in raw_applies if str(item).strip()],
        "expiresOn": _iso_date(front.get("expires_on")),
        "effectiveDate": _iso_date(front.get("effective_date")),
        "language": front.get("language") or "en",
        "sourceFile": filename,
    }
    return {key: value for key, value in meta.items() if value not in (None, "", [])}


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def inactive_reason(doc_meta: dict[str, Any] | None, *, today: dt.date | None = None) -> str | None:
    """切片是否应置 `disabled`：归档/过期文档入库但不可检索（PRD §8 元数据约定）。"""
    meta = doc_meta or {}
    status = str(meta.get("status") or "").lower()
    if status in _INACTIVE_STATUSES:
        return f"status={status}"
    expires = _iso_date(meta.get("expiresOn"))
    if not expires:
        return None
    try:
        expiry = dt.date.fromisoformat(expires[:10])
    except ValueError:
        logger.warning("expiresOn 不是合法日期，按长期有效处理：%s", expires)
        return None
    if expiry < (today or dt.date.today()):
        return f"expiresOn={expires}"
    return None


class IngestError(Exception):
    """入库失败（可读原因会写进 documents.meta_json.error 与 job.error）。"""


# 小于该长度的切片不构成证据（封面残片、目录字样），解析时丢弃
MIN_DRAFT_CHARS = 20


@dataclass
class IngestResult:
    document_id: int
    chunks: int
    pages: int
    used_toc: bool
    tokens: int
    elapsed_ms: int
    embedding_model: str
    embedding_dim: int


def evidence_id_for(source_type: str, document_id: int, seq: int) -> str:
    """证据 ID：`M01001` = 手册 + 文档 01 + 第 001 片。

    前缀随**资料分类**走（手册 M / 工单 T / FAQ F / 图纸 D / 表格 X），
    所以这里收的是 category；文件格式（pdf/docx）先由 `category_for` 映射成分类。
    后两段保证全局唯一且可排序；长度与原型里的 `M642` 同量级，后台与浮层都能直接展示。
    """
    prefix = EVIDENCE_ID_PREFIX.get(source_type, DEFAULT_EVIDENCE_PREFIX)
    return f"{prefix}{document_id:02d}{seq:03d}"


_OFFICE_TEXT_SOURCES: frozenset[str] = frozenset({"docx", "md", "txt"})
_TABLE_SOURCES: frozenset[str] = frozenset({"excel", "csv"})
_SUPPORTED_SOURCES: frozenset[str] = _OFFICE_TEXT_SOURCES | {"pdf"} | _TABLE_SOURCES

# 原件区目录名：上传与批量建档都往这里落盘。`file_path` 必须落在这层下面，
# 否则 `/admin/documents/{id}/file` 的白名单校验会拒（预览 404）。
ORIGINAL_DOCS_DIR = "original-docs"

# 后缀 → source_type：**上传接口与批量建档共用这一张表**，新增格式只改这里。
# 契约里的格式枚举（contracts/openapi.yaml）与 documents.source_type 的 check 约束要同步。
SUFFIX_TO_SOURCE: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".xlsx": "excel",
    ".csv": "csv",
}

_UNSAFE_NAME_CHARS = re.compile(r"[^\w\u4e00-\u9fff.\-()（）]+")


def original_target_path(data_dir: str | Path, filename: str) -> Path:
    """原件落盘路径：`<data>/original-docs/<秒级时间戳>-<安全文件名>`。

    时间戳前缀解决"同名不同内容"互相覆盖的问题（上传与批量导入共用同一套命名）；
    同一秒内同名再追加序号兜底，返回路径保证当下不存在，调用方可直接写入。
    """
    directory = (Path(data_dir) / ORIGINAL_DOCS_DIR).resolve()
    safe = _UNSAFE_NAME_CHARS.sub("_", Path(filename).name) or "upload"
    stamp = int(time.time())
    candidate = directory / f"{stamp}-{safe}"
    serial = 1
    while candidate.exists():
        serial += 1
        candidate = directory / f"{stamp}-{serial}-{safe}"
    return candidate


_FORMAT_TO_CATEGORY: dict[str, str] = {
    "pdf": "manual",
    "docx": "manual",
    "md": "manual",
    "txt": "manual",
    "xlsx": "table",
    "excel": "faq",  # 未显式指定分类时按 faq 归档（兼容旧数据/批量导入）
    "csv": "table",
    "ticketpkg": "ticket",
    "drawingindex": "drawing",
}


def category_for(source_type: str, meta: dict[str, Any] | None) -> str:
    """资料分类：优先取建档时写入的 category，否则按格式给默认分类。"""
    meta = meta or {}
    category = meta.get("category")
    if isinstance(category, str) and category in EVIDENCE_ID_PREFIX:
        return category
    return _FORMAT_TO_CATEGORY.get(source_type, source_type)


def resolve_document_path(settings: Settings, file_path: str) -> Path:
    """文档路径一律相对 `DATA_DIR`（容器内是 /app/data），不接受绝对路径以防越权读取。"""
    if not file_path:
        raise IngestError("文档缺少 file_path")
    candidate = Path(file_path)
    if candidate.is_absolute():
        raise IngestError("file_path 必须是相对 data/ 的路径")
    root = Path(settings.data_dir).resolve()
    resolved = (root / candidate).resolve()
    if root not in resolved.parents and resolved != root:
        raise IngestError("file_path 越出 data/ 目录")
    return resolved


def _vector_literal(vector: list[float]) -> str:
    """转 pgvector 文本格式 `[0.1,0.2,...]`；用字符串拼接避免逐位精度抖动。"""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def _tokenize(text: str) -> str:
    """jieba 预分词（S3 的 BM25 直接用这份，避免重复切词）。"""
    import jieba

    return " ".join(token for token in jieba.cut_for_search(text) if token.strip())


def parse_document(
    conn: psycopg.Connection,
    *,
    document_id: int,
    settings: Settings,
    embedder: EmbeddingClient,
) -> IngestResult:
    """把一份已建档的 PDF 解析并落成切片。失败抛 IngestError，并已把状态置 failed。"""
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(
            "select id, source_type, title, file_path, batch, manual_version, meta_json "
            "from documents where id = %s",
            (document_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise IngestError(f"文档不存在：{document_id}")
    doc_id, source_type, title, file_path, batch, manual_version, meta_json = row
    category = category_for(source_type, meta_json)

    if source_type not in _SUPPORTED_SOURCES:
        message = (
            "暂只支持 PDF / Word(.docx) / Markdown / TXT / FAQ Excel / 数据表格(Excel·CSV)"
            f"（当前 {source_type}）"
        )
        _mark_failed(conn, doc_id, message)
        raise IngestError(message)

    path = resolve_document_path(settings, file_path)
    if not path.exists():
        _mark_failed(conn, doc_id, f"原件不存在：{file_path}")
        raise IngestError(f"原件不存在：{file_path}")

    excel_info: dict[str, Any] | None = None
    table_info: dict[str, Any] | None = None
    if source_type in _TABLE_SOURCES:
        if category in {"table", "ticket"}:
            # 通用数据表（参数 / 规格 / 台账）与工单导出：一行一条记录，正文「列名：值」
            try:
                drafts, table_info = parse_table(
                    path,
                    max_rows=settings.doc_ingest_table_max_rows,
                    chunk_rows=settings.doc_ingest_table_chunk_rows,
                    first_column_section=settings.doc_ingest_table_first_column_section,
                )
            except ValueError as exc:
                _mark_failed(conn, doc_id, str(exc))
                raise IngestError(str(exc)) from exc
        elif source_type == "excel":
            # FAQ 表格：问题/答案列 + 可选行级批次
            try:
                drafts, excel_info = parse_faq_xlsx(path, batch_keys=frozenset())
            except ValueError as exc:
                _mark_failed(conn, doc_id, str(exc))
                raise IngestError(str(exc)) from exc
        else:
            message = "CSV 只支持「数据表格」分类（参数表 / 清单）；FAQ 请另存为 .xlsx 上传"
            _mark_failed(conn, doc_id, message)
            raise IngestError(message)
        parsed_pages = 0
        section_count = 0
        used_toc = False
    elif source_type in _OFFICE_TEXT_SOURCES:
        # Word / Markdown / TXT：没有可靠页码，出处只用章节；切片规则与 PDF 相同。
        # Markdown 语料带 YAML front matter：它属于元数据而不是正文，必须先剥掉，
        # 否则会被切进第一条证据、再被当成"原文"引用给客户。
        if source_type in {"md", "txt"}:
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            _front, body = parse_front_matter(raw_text)
            if body != raw_text:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=path.suffix, delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(body)
                    body_path = Path(handle.name)
                try:
                    parsed = parse_plaintext(body_path)
                finally:
                    body_path.unlink(missing_ok=True)
            else:
                parsed = parse_plaintext(path)
        else:
            parsed = parse_docx(path)
        drafts = chunk_document(
            parsed,
            max_chars=settings.doc_ingest_chunk_max_chars,
            min_chars=settings.doc_ingest_chunk_min_chars,
        )
        drafts = [draft for draft in drafts if len(draft.text) >= MIN_DRAFT_CHARS]
        parsed_pages = 0
        section_count = len(parsed.sections)
        used_toc = False
    else:
        parsed = parse_pdf(path)
        if is_probably_scanned(parsed) and settings.doc_ingest_reject_scanned:
            _mark_failed(conn, doc_id, "疑似扫描件：几乎无可提取文字，需要 OCR 后再入库")
            raise IngestError("疑似扫描件，需要 OCR")

        drafts = chunk_document(
            parsed,
            max_chars=settings.doc_ingest_chunk_max_chars,
            min_chars=settings.doc_ingest_chunk_min_chars,
        )
        # 封面残片（"目录""公司名"这类）不构成证据，丢弃；阈值很低，避免误删短问答
        drafts = [draft for draft in drafts if len(draft.text) >= MIN_DRAFT_CHARS]
        parsed_pages = parsed.page_count
        section_count = len(parsed.sections)
        used_toc = parsed.used_toc
    if not drafts:
        _mark_failed(conn, doc_id, "解析成功但没有得到任何有效切片（可能是空白文件）")
        raise IngestError("没有可入库的切片")

    texts = [draft.text for draft in drafts]
    vectors = embedder.embed(texts)
    embed_stats = getattr(embedder, "last", None)
    if len(vectors) != len(drafts):
        _mark_failed(conn, doc_id, "向量条数与切片数不一致")
        raise IngestError("向量条数与切片数不一致")

    # 入库事务：先删旧切片再整体写入，保证重复解析不翻倍
    doc_meta: dict[str, Any] = meta_json or {}
    blocked_reason = inactive_reason(doc_meta)
    # 文档级元数据下放到每条切片：检索后置过滤（国家 / 生效日期）只看切片，
    # 不用回表 join documents，也不会因为文档改了元数据而出现"切片看不出来源"。
    inherited_meta = {
        key: doc_meta[key]
        for key in (
            "docId",
            "category",
            "version",
            "appliesTo",
            "expiresOn",
            "effectiveDate",
            "status",
            "language",
        )
        if doc_meta.get(key) not in (None, "", [])
    }
    with conn.cursor() as cur:
        cur.execute("delete from chunks where document_id = %s", (doc_id,))
        for draft, vector in zip(drafts, vectors, strict=True):
            # 行级批次优先；行内没填则继承文档批次（两者都空 = 不限批次）
            chunk_batch = draft.batch or batch
            chunk_meta: dict[str, Any] = {
                "heading": draft.heading,
                "loc": draft.loc,
                **inherited_meta,
                **draft.meta,
            }
            if draft.batch:
                # 标记「这个批次来自行内」，文档级改批次时不覆盖它
                chunk_meta["batchFromRow"] = True
            if blocked_reason:
                # 归档/过期：入库留档，但不可检索（chunks.disabled 是检索的硬过滤条件）
                chunk_meta["disabledReason"] = blocked_reason
            cur.execute(
                """
                insert into chunks
                    (document_id, evidence_id, content, section_no, page_from, page_to,
                     batch, manual_version, meta_json, embedding, bm25_tokens, disabled)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector, %s, %s)
                """,
                (
                    doc_id,
                    evidence_id_for(category, doc_id, draft.seq),
                    draft.text,
                    draft.section_no,
                    draft.page_from,
                    draft.page_to,
                    chunk_batch,
                    manual_version,
                    psycopg.types.json.Jsonb(chunk_meta),
                    _vector_literal(vector),
                    _tokenize(draft.text),
                    bool(blocked_reason),
                ),
            )
        meta = {
            "pages": parsed_pages,
            "sections": section_count,
            "usedToc": used_toc,
            "chunks": len(drafts),
            "embeddingModel": settings.embedding_model,
            "embeddingDim": settings.embedding_dim,
            "embeddingMs": getattr(embed_stats, "elapsed_ms", None),
            "parsedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if blocked_reason:
            meta["disabledChunks"] = len(drafts)
            meta["disabledReason"] = blocked_reason
        if excel_info is not None:
            meta.update(
                {
                    "rows": excel_info["rows"],
                    "sheet": excel_info["sheet"],
                    "faq": True,
                    # 行级批次条数：治理页能看出「这份 FAQ 有 N 行自带批次」
                    "rowBatches": sum(1 for draft in drafts if draft.batch),
                }
            )
        if table_info is not None:
            meta.update(
                {
                    "table": True,
                    "rows": table_info["rows"],
                    "sheets": table_info["sheets"],
                    "tableFormat": table_info["format"],
                    "tableChunkRows": table_info["chunkRows"],
                    "tableChunks": table_info["chunks"],
                    **({"ticketExport": True} if category == "ticket" else {}),
                }
            )
        cur.execute(
            "update documents set status = 'parsed', meta_json = meta_json || %s::jsonb, "
            "updated_at = now() where id = %s",
            (psycopg.types.json.Jsonb(meta), doc_id),
        )
    conn.commit()

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "parsed doc %s: pages=%d sections=%d chunks=%d embed_ms=%s total_ms=%d",
        doc_id,
        parsed_pages,
        section_count,
        len(drafts),
        getattr(embed_stats, "elapsed_ms", None),
        elapsed_ms,
    )
    return IngestResult(
        document_id=doc_id,
        chunks=len(drafts),
        pages=parsed_pages,
        used_toc=used_toc,
        tokens=sum(len(draft.text) for draft in drafts),
        elapsed_ms=elapsed_ms,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
    )


def _mark_failed(conn: psycopg.Connection, document_id: int, reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update documents set status = 'failed', meta_json = meta_json || %s::jsonb, "
            "updated_at = now() where id = %s",
            (psycopg.types.json.Jsonb({"error": reason}), document_id),
        )
    conn.commit()
    logger.warning("doc %s failed: %s", document_id, reason)


# ---------- 建档 ----------
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def infer_metadata(filename: str, *, path_category: str | None = None) -> dict[str, Any]:
    """从文件名推断型号/版本/分类（拆尽已知命名规则，推不出就留空，不猜）。

    实测命名形态：`BK系列制动器维护保养手册Ver.3.pdf`、`DZE-12E制动器维护保养手册Ver.3.pdf`。
    Excel FAQ 不是按型号归档的资料，文件名里的型号/版本一律不推断（`faq-xxx.xlsx`
    里的 `faq-` 会被 "-" 规则误当成型号）。

    表格类（.xlsx / .csv）的分类优先级：
    1. `path_category`（批量建档按暂存区目录推断：`inbox/faq` / `inbox/ticket`）；
    2. 文件名含 FAQ / 常见问题 / 问答 → `faq`；含「工单」→ `ticket`；
    3. 其余默认 `table`（数据表格一行一条）——`table` 才是通用台账/记录表的正确语义。
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls", ".csv"}:
        category = path_category
        if category is None:
            lowered = stem.lower()
            if "faq" in lowered or "常见问题" in stem or "问答" in stem:
                category = "faq"
            elif "工单" in stem:
                category = "ticket"
            else:
                category = "table"
        return {"model": None, "version": None, "category": category}
    model = None
    version = None
    import re

    for marker in ("系列", "-", "_"):
        if marker in stem:
            head = stem.split(marker)[0].strip()
            # 编号前缀（"1-1_""00_""3-1_"）不是型号：型号必须含英文字母，
            # 否则批量导入的文档会把「1」「00」「3」写进 device_model，列表与过滤都错。
            if head and len(head) <= 16 and re.search(r"[A-Za-z]", head):
                model = head + (marker if marker == "系列" else "")
                break

    match = re.search(r"Ver\.?\s*(\d+(?:\.\d+)*)", stem, re.IGNORECASE)
    if match:
        version = f"Ver.{match.group(1)}"
    category = "manual"
    if "工单" in stem:
        category = "ticket"
    elif any(word in stem for word in ("FAQ", "常见问题", "问答")):
        category = "faq"
    elif any(word in stem for word in ("图", "图纸")):
        category = "drawing"
    return {"model": model or None, "version": version, "category": category}


def upsert_document(
    conn: psycopg.Connection,
    *,
    title: str,
    file_path: str,
    source_type: str,
    sha256: str,
    meta: dict[str, Any],
    size_bytes: int | None = None,
    doc_code: str | None = None,
    effective_date: dt.date | None = None,
) -> tuple[int, bool]:
    """按 sha256 去重建档。返回 (document_id, 是否新建)。

    `size_bytes` 落 meta_json.sizeBytes：列表页要显示"18.6 MB"这类体积；
    `meta` 里除 category 之外的字段（docId/appliesTo/expiresOn/status/language…）
    整体并入 meta_json，切片解析时再下放到每条证据，供检索后置过滤使用。
    """
    with conn.cursor() as cur:
        cur.execute("select id from documents where file_sha256 = %s", (sha256,))
        row = cur.fetchone()
        if row:
            return int(row[0]), False
        doc_meta: dict[str, Any] = {"category": meta.get("category", "manual")}
        doc_meta.update(
            {
                key: value
                for key, value in meta.items()
                if key != "category" and value not in (None, "", [])
            }
        )
        if size_bytes is not None:
            doc_meta["sizeBytes"] = int(size_bytes)
        cur.execute(
            """
            insert into documents
                (source_type, title, doc_code, device_model, manual_version, effective_date,
                 file_path, file_sha256, status, meta_json)
            values (%s, %s, %s, %s, %s, %s, %s, %s, 'uploaded', %s::jsonb)
            returning id
            """,
            (
                source_type,
                title,
                doc_code,
                meta.get("model"),
                meta.get("version"),
                effective_date,
                file_path,
                sha256,
                psycopg.types.json.Jsonb(doc_meta),
            ),
        )
        document_id = int(cur.fetchone()[0])
    conn.commit()
    return document_id, True


def dump_meta(meta: dict[str, Any]) -> str:
    return json.dumps(meta, ensure_ascii=False)
