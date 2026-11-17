"""工具编排：选工具 → 执行 → 用**确定性模板**把事实翻译成客户能看的话。

为什么成功路径也不调 LLM：
事实（订单状态、物流节点、预计送达）只有工具返回值一个来源；让模型"润色"一次，
就等于给它一次编造数字的机会。这里用固定模板拼接，字段缺了就写"未提供"。

失败分级（PRD §4.1）：
- `not_found`：单号不存在 → 提示核对，**不转人工**（这是我们能确定的事情）
- `timeout` / `error`：查不到 → 固定降级话术 + **强制转人工**，绝不让模型猜一个到达时间
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import psycopg

from modules.tools import get_tool, run
from modules.tools.types import TOOL_ERROR, TOOL_NOT_FOUND, TOOL_OK, TOOL_TIMEOUT, ToolOutcome

DEGRADED_TEXT = (
    "I can't check your order right now: our order system didn't respond in time. "
    "Rather than guess an arrival date, I'm connecting you to a human agent who can see "
    "the live status."
)
NOT_FOUND_TEXT = (
    "I couldn't find that order number in our system. Please double-check it — order numbers "
    "look like WB-10001. If you don't have it, a human agent can look it up for you."
)

# 异常状态的固定补充说明（不推测、不承诺）
ANOMALY_NOTES: dict[str, str] = {
    "customs_hold": (
        "It's currently held at customs, which is why tracking stopped moving. "
        "Customs delays are outside our control; the carrier will resume delivery once released."
    ),
    "lost": (
        "The carrier has marked this shipment as lost. A human agent will arrange a "
        "replacement or refund — refunds are never approved by this assistant."
    ),
    "wrong_address": (
        "The delivery address on this order looks incomplete or incorrect. "
        "Address changes must be made by a human agent before dispatch."
    ),
}

STATUS_LABELS: dict[str, str] = {
    "pending": "awaiting payment confirmation",
    "pending_payment": "awaiting payment",
    "unshipped": "not shipped yet",
    "shipped": "shipped",
    "delivered": "delivered",
    "cancelled": "cancelled",
    "refunded": "refunded",
}


@dataclass(frozen=True)
class ToolPlan:
    name: str
    arg_name: str
    value: str


def plan(structured: dict[str, Any]) -> ToolPlan | None:
    """按意图与已有槽位决定是否调工具；缺参数就返回 None（交给追问卡）。"""
    intent = structured.get("intent")
    order_no = structured.get("orderNo")
    tracking_no = structured.get("trackingNo")
    if intent == "logistics":
        if tracking_no:
            return ToolPlan("query_logistics", "tracking_no", str(tracking_no))
        if order_no:
            return ToolPlan("query_order", "order_no", str(order_no))
        return None
    if intent == "order" and order_no:
        return ToolPlan("query_order", "order_no", str(order_no))
    return None


def invoke(
    conn: psycopg.Connection,
    tool_plan: ToolPlan,
    *,
    timeout_s: float,
    fault: str | None = None,
) -> ToolOutcome:
    """执行工具；`fault` 仅用于演示注入（`tool_timeout` / `not_found`）。"""
    fn, arg_name = get_tool(tool_plan.name)

    def call() -> dict[str, Any] | None:
        if fault == "tool_timeout":
            time.sleep(timeout_s + 0.3)  # 演示：稳定触发超时
        if fault == "not_found":
            return None
        return fn(conn, tool_plan.value)

    return run(call, timeout_s=timeout_s)


def compose_answer(outcome: ToolOutcome, plan_: ToolPlan) -> str:
    """把工具结果拼成回答（确定性、只用返回字段）。"""
    if outcome.status == TOOL_NOT_FOUND:
        return NOT_FOUND_TEXT
    if outcome.status in {TOOL_TIMEOUT, TOOL_ERROR}:
        return DEGRADED_TEXT
    data = outcome.data or {}
    if plan_.name == "query_logistics":
        return _compose_logistics(data)
    return _compose_order(data)


def _compose_order(data: dict[str, Any]) -> str:
    order_no = data.get("orderNo") or "this order"
    status = STATUS_LABELS.get(str(data.get("status")), str(data.get("status") or "unknown"))
    lines = [f"Order {order_no} is currently {status}."]
    payment = data.get("paymentStatus")
    if payment:
        lines.append(f"Payment status: {payment}.")
    address = data.get("address") or {}
    city = address.get("city")
    country = address.get("country") or data.get("country")
    if city or country:
        lines.append(f"Shipping to: {', '.join(part for part in (city, country) if part)}.")
    shipment = data.get("shipment") or {}
    tracking = data.get("trackingNo")
    if tracking:
        carrier = shipment.get("carrier") or "the carrier"
        node = shipment.get("currentNode")
        eta = shipment.get("eta")
        line = f"Tracking {tracking} via {carrier}."
        if node:
            line += f" Latest update: {node}."
        if eta:
            line += f" Estimated delivery: {eta}."
        lines.append(line)
    anomaly = data.get("anomaly") or shipment.get("anomaly")
    if anomaly in ANOMALY_NOTES:
        lines.append(ANOMALY_NOTES[anomaly])
    return "\n".join(lines)


def _compose_logistics(data: dict[str, Any]) -> str:
    tracking = data.get("trackingNo") or "this shipment"
    lines = [f"Shipment {tracking}."]
    carrier = data.get("carrier")
    if carrier:
        lines.append(f"Carrier: {carrier}.")
    node = data.get("currentNode")
    if node:
        lines.append(f"Latest update: {node}.")
    eta = data.get("eta")
    if eta:
        lines.append(f"Estimated delivery: {eta}.")
    events = data.get("events") or []
    recent = events[-3:]
    for event in recent:
        at = event.get("at")
        label = event.get("node")
        if label:
            lines.append(f"- {at}: {label}" if at else f"- {label}")
    anomaly = data.get("anomaly")
    if anomaly in ANOMALY_NOTES:
        lines.append(ANOMALY_NOTES[anomaly])
    return "\n".join(lines)


def event_payload(outcome: ToolOutcome, plan_: ToolPlan) -> dict[str, Any]:
    """SSE `tool` 事件的载荷（前端据此显示"查了/超时/没查到"）。"""
    payload: dict[str, Any] = {
        "name": plan_.name,
        "status": outcome.status,
        "latencyMs": outcome.latency_ms,
        "args": {plan_.arg_name: plan_.value},
    }
    if outcome.status == TOOL_OK and outcome.data:
        payload["summary"] = outcome.data.get("currentNode") or outcome.data.get("status")
    return payload


def is_ok(outcome: ToolOutcome) -> bool:
    return outcome.status == TOOL_OK
