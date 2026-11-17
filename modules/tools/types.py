"""工具调用层的数据形状（公共层新增包，不依赖 app）。

状态只有四种，刻意不扩展：
- `ok`：拿到数据
- `not_found`：单号不存在（可提示核对，不必转人工）
- `timeout`：超时（必须降级 + 转人工，禁止模型推测）
- `error`：其他异常（同上）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TOOL_OK = "ok"
TOOL_NOT_FOUND = "not_found"
TOOL_TIMEOUT = "timeout"
TOOL_ERROR = "error"

TOOL_STATUSES = (TOOL_OK, TOOL_NOT_FOUND, TOOL_TIMEOUT, TOOL_ERROR)


@dataclass
class ToolOutcome:
    """一次工具调用的结果；`data` 只放"能核准的事实"，不放自由文本。"""

    status: str
    data: dict[str, Any] | None = None
    latency_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == TOOL_OK

    def to_log(self) -> dict[str, Any]:
        """落库/导出用的摘要（不写整行数据，避免把演示数据抄两遍）。"""
        summary: dict[str, Any] = {"status": self.status, "latencyMs": self.latency_ms}
        if self.data:
            summary["fields"] = sorted(self.data.keys())
            summary["orderNo"] = self.data.get("orderNo")
            summary["trackingNo"] = self.data.get("trackingNo")
        if self.error:
            summary["error"] = self.error
        return summary
