"""工具层：mock 查询、超时归一、白名单边界（需要数据库）。"""

from __future__ import annotations

import time

import pytest
from conftest import requires_db

from modules.tools import (
    TOOL_NOT_FOUND,
    TOOL_OK,
    TOOL_TIMEOUT,
    WRITE_REQUESTS,
    ToolNotAllowed,
    get_tool,
    query_logistics,
    query_order,
    run,
    tool_names,
)

pytestmark = requires_db


def test_query_order_returns_seeded_facts(conn) -> None:
    data = query_order(conn, "WB-10001")
    assert data is not None
    assert data["orderNo"] == "WB-10001"
    assert data["country"] == "US"
    assert data["status"] == "shipped"
    assert data["trackingNo"] == "1Z1000100000000001"
    assert data["shipment"]["currentNode"]


def test_query_order_is_case_insensitive(conn) -> None:
    assert query_order(conn, "wb-10001") is not None


def test_query_order_not_found_returns_none(conn) -> None:
    assert query_order(conn, "WB-99999") is None


def test_query_logistics_customs_hold(conn) -> None:
    data = query_logistics(conn, "1Z1000300000000003")
    assert data is not None
    assert data["anomaly"] == "customs_hold"
    assert "customs" in data["currentNode"].lower()


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(ToolNotAllowed):
        get_tool("issue_refund")


def test_write_requests_have_no_tool() -> None:
    """写入类诉求不提供工具：白名单里不存在退款/取消/改地址。"""
    assert set(WRITE_REQUESTS).isdisjoint(set(tool_names()))


def test_run_ok_wraps_dict() -> None:
    outcome = run(lambda: {"orderNo": "WB-10001"}, timeout_s=1.0)
    assert outcome.status == TOOL_OK
    assert outcome.data == {"orderNo": "WB-10001"}
    assert outcome.latency_ms >= 0


def test_run_not_found_wraps_none() -> None:
    outcome = run(lambda: None, timeout_s=1.0)
    assert outcome.status == TOOL_NOT_FOUND


def test_run_timeout_does_not_block_caller() -> None:
    def slow() -> dict:
        time.sleep(0.6)
        return {"orderNo": "WB-10001"}

    started = time.perf_counter()
    outcome = run(slow, timeout_s=0.2)
    elapsed = time.perf_counter() - started

    assert outcome.status == TOOL_TIMEOUT
    assert elapsed < 0.5, "超时必须立刻返回，不能等工具线程跑完"
    assert outcome.error


def test_run_error_is_normalized() -> None:
    def boom() -> dict:
        raise RuntimeError("db down")

    outcome = run(boom, timeout_s=1.0)
    assert outcome.status == "error"
    assert "RuntimeError" in (outcome.error or "")
