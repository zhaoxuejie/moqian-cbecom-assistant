"""公共工具调用层（新增包）：只读的 mock 订单/物流查询 + 超时归一 + 白名单。

设计边界（见实施计划 D-C1）：
- 这里只有"机制"：怎么查、怎么超时、哪些工具存在；
- "查到了怎么跟客户说"是行业话术，放 `app/`；
- **没有 function calling**：路由与取参由 `app/intent.py` 的规则完成，
  本包不感知模型，也不做任何推断。
"""

from .execute import run
from .mock_repo import query_logistics, query_order
from .registry import READ_TOOLS, WRITE_REQUESTS, ToolNotAllowed, get_tool, tool_names
from .types import (
    TOOL_ERROR,
    TOOL_NOT_FOUND,
    TOOL_OK,
    TOOL_STATUSES,
    TOOL_TIMEOUT,
    ToolOutcome,
)

__all__ = [
    "READ_TOOLS",
    "WRITE_REQUESTS",
    "TOOL_ERROR",
    "TOOL_NOT_FOUND",
    "TOOL_OK",
    "TOOL_STATUSES",
    "TOOL_TIMEOUT",
    "ToolNotAllowed",
    "ToolOutcome",
    "get_tool",
    "query_logistics",
    "query_order",
    "run",
    "tool_names",
]
