"""执行器：给工具调用加超时与异常归一。

为什么用线程 + join 而不是 asyncio：`/chat` 是同步生成器，工具本身是同步 SQL；
线程是最小侵入的做法，且跨平台（Windows 没有 SIGALRM）。

**超时后不能等线程结束**：daemon 线程继续跑，但调用方立刻拿到 `timeout` 并走降级话术。
这正是我们要的——宁可说"查不到，请稍后/转人工"，也不能让访客等到 30 秒后看到一个编造的答案。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .types import TOOL_ERROR, TOOL_NOT_FOUND, TOOL_OK, TOOL_TIMEOUT, ToolOutcome


def run(
    fn: Callable[[], Any],
    *,
    timeout_s: float,
) -> ToolOutcome:
    """跑一个工具调用；`fn` 返回 dict（查不到返回 None）。"""
    started = time.perf_counter()
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - 统一归一成 error
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True, name="tool-call")
    worker.start()
    worker.join(timeout_s)
    latency = int((time.perf_counter() - started) * 1000)

    if worker.is_alive():
        return ToolOutcome(
            status=TOOL_TIMEOUT,
            data=None,
            latency_ms=latency,
            error=f"tool exceeded {timeout_s:g}s",
        )
    if "error" in box:
        return ToolOutcome(
            status=TOOL_ERROR,
            data=None,
            latency_ms=latency,
            error=f"{type(box['error']).__name__}: {box['error']}",
        )
    value = box.get("value")
    if value is None:
        return ToolOutcome(status=TOOL_NOT_FOUND, data=None, latency_ms=latency)
    if isinstance(value, dict):
        return ToolOutcome(status=TOOL_OK, data=value, latency_ms=latency)
    return ToolOutcome(
        status=TOOL_ERROR,
        data=None,
        latency_ms=latency,
        error=f"unexpected tool return type: {type(value).__name__}",
    )
