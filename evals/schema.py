"""评测集 schema 与加载（跨境分层版）。

评测集是"问题 + 期望行为"的清单。跨境版在原有字段上加了三类断言：

- `layer`：A 政策 / B 工具 / C 硬转 / D 拒答 / E 多语言（分层报告与指标都由它驱动）；
- `expect.tool`：期望调用的工具与结果状态（`ok/not_found/timeout/error`）；
- `expect.handoff`：期望转人工及类别（退款/投诉/法务/账户安全）；
- `expect.lang`：回答语言；`expect.citationsMin`：最少引用数；
- `expect.noArchived`：不得引用归档/过期切片（"旧版 60 天"那条的关键断言）；
- `fixture.fault`：演示故障注入（`tool_timeout` / `not_found`），仅评测使用。

`keywords` 只用于**检索命中**判定，不是"答案必须包含这些词"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LAYERS: tuple[str, ...] = ("A", "B", "C", "D", "E")
LAYER_NAMES = {
    "A": "政策 RAG（引用可核验）",
    "B": "工具调用（订单/物流/失败降级）",
    "C": "硬转人工（四类）",
    "D": "应拒答/缺口",
    "E": "多语言输出",
}
TOOL_STATUSES = ("ok", "not_found", "timeout", "error")
FAULTS = ("tool_timeout", "not_found")


class EvalCaseError(ValueError):
    """评测集格式错误：宁可加载失败，也不要"跳过几条"悄悄算分。"""


@dataclass
class Case:
    id: str
    question: str
    layer: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    expect_refuse: bool = False
    expect_safe: bool = False
    keywords: list[str] = field(default_factory=list)
    expect_tool_name: str | None = None
    expect_tool_status: str | None = None
    expect_handoff: bool = False
    expect_handoff_category: str | None = None
    expect_degraded: bool = False
    expect_lang: str | None = None
    expect_citations_min: int = 0
    expect_no_archived: bool = False
    fixture: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


def _parse_tool(expect: dict[str, Any], case_id: str) -> tuple[str | None, str | None]:
    tool = expect.get("tool")
    if tool is None:
        return None, None
    if not isinstance(tool, dict):
        raise EvalCaseError(f"{case_id} 的 expect.tool 必须是对象")
    name = str(tool.get("name") or "").strip() or None
    status = str(tool.get("status") or "").strip() or None
    if status and status not in TOOL_STATUSES:
        raise EvalCaseError(f"{case_id} 的 expect.tool.status 非法：{status}")
    return name, status


def _parse_handoff(expect: dict[str, Any], case_id: str) -> tuple[bool, str | None]:
    handoff = expect.get("handoff")
    if handoff is None:
        return False, None
    if isinstance(handoff, bool):
        return handoff, None
    if not isinstance(handoff, dict):
        raise EvalCaseError(f"{case_id} 的 expect.handoff 必须是布尔或对象")
    required = bool(handoff.get("required", True))
    category = str(handoff.get("category") or "").strip() or None
    return required, category


def load_cases(path: str | Path) -> list[Case]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise EvalCaseError("评测集根节点必须是列表")
    cases: list[Case] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise EvalCaseError(f"第 {index} 条不是对象")
        case_id = str(item.get("id") or "").strip()
        question = str(item.get("question") or "").strip()
        if not case_id or not question:
            raise EvalCaseError(f"第 {index} 条缺少 id 或 question")
        if case_id in seen:
            raise EvalCaseError(f"重复的案例 id：{case_id}")
        seen.add(case_id)

        layer = str(item.get("layer") or "").strip().upper() or None
        if layer is not None and layer not in LAYERS:
            raise EvalCaseError(f"{case_id} 的 layer 非法：{layer}（必须是 A/B/C/D/E）")

        expect = item.get("expect") or {}
        expect_refuse = bool(expect.get("refuse", False))
        expect_safe = bool(expect.get("safe", False))
        expect_degraded = bool(expect.get("degraded", False))
        if expect_refuse and expect_safe:
            raise EvalCaseError(f"{case_id} 不能同时期望拒答与安全拦截")
        if expect_degraded and expect_refuse:
            raise EvalCaseError(f"{case_id} 降级用例不需要再写 refuse（降级本身就是拒答）")
        tool_name, tool_status = _parse_tool(expect, case_id)
        handoff_required, handoff_category = _parse_handoff(expect, case_id)
        keywords = [str(word) for word in (expect.get("keywords") or []) if str(word).strip()]

        fixture = dict(item.get("fixture") or {})
        fault = fixture.get("fault")
        if fault and fault not in FAULTS:
            raise EvalCaseError(f"{case_id} 的 fixture.fault 非法：{fault}")

        cases.append(
            Case(
                id=case_id,
                question=question,
                layer=layer,
                context=dict(item.get("context") or {}),
                expect_refuse=expect_refuse,
                expect_safe=expect_safe,
                keywords=keywords,
                expect_tool_name=tool_name,
                expect_tool_status=tool_status,
                expect_handoff=handoff_required,
                expect_handoff_category=handoff_category,
                expect_degraded=expect_degraded,
                expect_lang=str(expect.get("lang") or "").strip().lower() or None,
                expect_citations_min=int(expect.get("citationsMin") or 0),
                expect_no_archived=bool(expect.get("noArchived", False)),
                fixture=fixture,
                note=item.get("note"),
            )
        )
    return cases


def count_by_layer(cases: list[Case]) -> dict[str, int]:
    counts = {layer: 0 for layer in LAYERS}
    for case in cases:
        if case.layer:
            counts[case.layer] += 1
    return counts
