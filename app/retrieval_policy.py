"""检索后置过滤：按「适用国家」与「生效/过期日期」收敛证据集。

为什么不改 `modules/retrieval`：公共层只做机制（dense + BM25 + RRF），它的过滤条件只有
`model/batch`；"哪些国家适用、哪一版还在生效"属于行业政策语义，应由 `app` 注入。
所以时序是：

    检索（modules） → 后置过滤（本文件） → 证据门（modules）

**过滤后必须重算证据门**：`evaluate_gate` 的 hits / top_score / coverage 都是在这批
证据上算的，少了几条就可能从"过"变成"不过"，这正是我们要的效果（宁缺毋滥）。

元数据来自 `chunks.meta_json`（P1-2 导入时把 `appliesTo / expiresOn / status` 下放到每条切片），
所以这里只查一次 `chunks` 表，不回表 join `documents`。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

logger = logging.getLogger("app.retrieval_policy")

_INACTIVE_STATUSES = frozenset({"archived", "expired", "superseded", "disabled"})


@dataclass
class PolicyDecision:
    """过滤结果：保留项、被丢弃项与原因、给前端看的一句话说明。"""

    kept: list[Any] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    note: str | None = None

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def reasons(self) -> str:
        counts: dict[str, int] = {}
        for _evidence_id, reason in self.dropped:
            counts[reason] = counts.get(reason, 0) + 1
        return ", ".join(f"{reason}={count}" for reason, count in sorted(counts.items()))


def _as_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _drop_reason(meta: dict[str, Any], *, country: str | None, today: dt.date) -> str | None:
    """一条切片该不该丢；返回原因（`None` = 保留）。"""
    status = str(meta.get("status") or "").lower()
    if status in _INACTIVE_STATUSES:
        return "inactive"
    expiry = _as_date(meta.get("expiresOn"))
    if expiry is not None and expiry < today:
        return "expired"
    if country:
        applies = meta.get("appliesTo") or []
        if isinstance(applies, str):
            applies = [part.strip() for part in applies.split(",")]
        normalized = {str(item).strip().upper() for item in applies if str(item).strip()}
        if normalized and country.strip().upper() not in normalized:
            return "country"
    return None


def filter_with_meta(
    items: list[Any],
    meta_by_id: dict[str, dict[str, Any]],
    *,
    country: str | None = None,
    today: dt.date | None = None,
) -> PolicyDecision:
    """纯函数版本：给每条 item 配上元数据后做过滤（不碰数据库，便于单测）。"""
    current = today or dt.date.today()
    decision = PolicyDecision()
    for item in items:
        evidence_id = getattr(item, "evidence_id", "")
        meta = meta_by_id.get(evidence_id) or {}
        reason = _drop_reason(meta, country=country, today=current)
        if reason:
            decision.dropped.append((evidence_id, reason))
        else:
            decision.kept.append(item)
    if decision.dropped:
        decision.note = (
            f"Filtered {decision.dropped_count} of {len(items)} passages "
            "by country or effective date."
        )
    return decision


def doc_codes(conn: psycopg.Connection, evidence_ids: list[str]) -> dict[str, str]:
    """证据 ID → 文档标识（front matter 的 `docId`，缺则用 document_id）。

    版本冲突必须按**同一份文档**比较；只看证据前缀会把"产品 FAQ"和"B2B FAQ"
    （同为 FQ）误判成同一政策的两版。
    """
    if not evidence_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.evidence_id, coalesce(c.meta_json->>'docId', 'doc-' || c.document_id)
              from chunks c
             where c.evidence_id = any(%s)
            """,
            (evidence_ids,),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def apply(
    conn: psycopg.Connection,
    items: list[Any],
    *,
    country: str | None = None,
    today: dt.date | None = None,
) -> PolicyDecision:
    """查一次 `chunks.meta_json`，然后按国家/日期过滤。"""
    if not items:
        return PolicyDecision()
    evidence_ids = [item.evidence_id for item in items]
    meta_by_id: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "select evidence_id, meta_json from chunks where evidence_id = any(%s)",
            (evidence_ids,),
        )
        for evidence_id, meta in cur.fetchall():
            if isinstance(meta, dict):
                meta_by_id[evidence_id] = meta
    decision = filter_with_meta(items, meta_by_id, country=country, today=today)
    if decision.dropped:
        logger.info(
            "retrieval policy: kept=%d dropped=%d (%s) country=%s",
            len(decision.kept),
            decision.dropped_count,
            decision.reasons(),
            country or "-",
        )
    return decision
