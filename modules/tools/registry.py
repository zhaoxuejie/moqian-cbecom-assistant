"""工具白名单与权限边界（PRD §4.1）。

规则只有一条，但很硬：**查询类可自主调用，写入类根本不提供工具**。
退款/取消/改地址不是"让 Agent 谨慎一点"，而是不存在可调用的入口——
这样模型再怎么被诱导，也没有可执行的写操作。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .mock_repo import query_logistics, query_order

# 允许 Agent 自主调用的只读工具
READ_TOOLS: dict[str, dict[str, Any]] = {
    "query_order": {
        "fn": query_order,
        "arg": "order_no",
        "label": "Look up an order",
    },
    "query_logistics": {
        "fn": query_logistics,
        "arg": "tracking_no",
        "label": "Look up a shipment",
    },
}

# 写入类诉求：不提供工具，只能引导 + 转人工（由 app 层话术承接）
WRITE_REQUESTS: tuple[str, ...] = (
    "refund",
    "cancel_order",
    "change_address",
)


class ToolNotAllowed(Exception):
    """调用了白名单之外的工具（正常路径不会发生，属于代码错误）。"""


def tool_names() -> tuple[str, ...]:
    return tuple(READ_TOOLS)


def get_tool(name: str) -> tuple[Callable[..., Any], str]:
    """返回 `(函数, 参数名)`；不在白名单直接抛错，不做兜底。"""
    spec = READ_TOOLS.get(name)
    if spec is None:
        raise ToolNotAllowed(f"tool not allowed: {name}")
    return spec["fn"], spec["arg"]
