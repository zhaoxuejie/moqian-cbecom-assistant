"""检索后置过滤（app 层）：按适用国家与生效日期收敛证据集（纯函数，不连库）。

与管理层 `tests/test_retrieval_policy.py` 的区别：那份测的是 `modules/retrieval`
按型号/批次过滤的策略；这份测的是跨境新增的**政策语义**过滤（哪个国家适用、哪版还在生效）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.retrieval_policy import filter_with_meta


@dataclass
class _Item:
    evidence_id: str


def _items(*evidence_ids: str) -> list[_Item]:
    return [_Item(evidence_id) for evidence_id in evidence_ids]


def test_country_filter_drops_non_applicable_policy() -> None:
    items = _items("RP424001", "RP425001", "SP423001")
    meta = {
        "RP424001": {"appliesTo": ["US", "UK", "DE", "FR", "ES"], "status": "current"},
        "RP425001": {"appliesTo": ["US"], "status": "current"},
        "SP423001": {"status": "current"},  # 没写适用国家 = 全球通用，保留
    }

    decision = filter_with_meta(items, meta, country="DE")

    assert [item.evidence_id for item in decision.kept] == ["RP424001", "SP423001"]
    assert decision.dropped == [("RP425001", "country")]
    assert decision.note is not None and "1 of 3" in decision.note


def test_country_match_is_case_insensitive() -> None:
    items = _items("RP1")
    meta = {"RP1": {"appliesTo": ["us"], "status": "current"}}
    assert filter_with_meta(items, meta, country="US").kept


def test_archived_and_expired_are_dropped_regardless_of_country() -> None:
    items = _items("A", "B", "C")
    meta = {
        "A": {"status": "archived"},
        "B": {"status": "current", "expiresOn": "2026-02-28"},
        "C": {"status": "current", "expiresOn": "2027-12-31"},
    }

    decision = filter_with_meta(items, meta, country="US", today=dt.date(2026, 10, 28))

    assert [item.evidence_id for item in decision.kept] == ["C"]
    reasons = {evidence_id: reason for evidence_id, reason in decision.dropped}
    assert reasons == {"A": "inactive", "B": "expired"}


def test_no_metadata_keeps_item_and_no_note() -> None:
    """没有元数据的切片（历史数据）不猜、不丢：过滤只做"确知不适用"的剔除。"""
    items = _items("X1")

    decision = filter_with_meta(items, {}, country="US")

    assert [item.evidence_id for item in decision.kept] == ["X1"]
    assert decision.dropped == []
    assert decision.note is None


def test_without_country_only_date_rules_apply() -> None:
    items = _items("A", "B")
    meta = {
        "A": {"appliesTo": ["US"], "status": "current"},
        "B": {"status": "archived"},
    }

    decision = filter_with_meta(items, meta, country=None)

    assert [item.evidence_id for item in decision.kept] == ["A"]
    assert decision.dropped == [("B", "inactive")]
