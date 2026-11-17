"""评测指标：只算能从结果对象**客观核验**的东西。

通用指标（沿用上游口径）：检索命中、引用覆盖率、拒答判断、安全拦截、P50/P90。
跨境新增**分层指标**（A–E），因为"用总均值掩盖分层问题"是评测最常见的自欺：

- A 政策：引用合规 + 不得引用归档/过期切片
- B 工具：该调的工具调了没、失败是否降级
- C 硬转：四类是否都转到人工
- D 拒答：应拒答却作答（区分"明示缺口"）
- E 多语言：回答语言是否与期望一致
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .schema import LAYERS


@dataclass
class CaseResult:
    id: str
    question: str
    expect_refuse: bool
    expect_safe: bool
    refused: bool
    gate_reason: str | None
    citations: int
    steps: int
    steps_with_citations: int
    stripped: list[str]
    latency_ms: int
    keywords: list[str]
    keyword_hits: list[str]
    evidence: list[str] = field(default_factory=list)
    gate_hits: int | None = None
    gate_top: float | None = None
    gate_coverage: float | None = None
    error: str | None = None
    disclosed: bool = False
    # ---- 跨境分层 ----
    layer: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    handoff_category: str | None = None
    handoff_rule: str | None = None
    lang: str | None = None
    citation_ids: list[str] = field(default_factory=list)
    archived_citations: int = 0
    expect_tool_name: str | None = None
    expect_tool_status: str | None = None
    expect_handoff: bool = False
    expect_handoff_category: str | None = None
    expect_degraded: bool = False
    expect_lang: str | None = None
    expect_citations_min: int = 0
    expect_no_archived: bool = False


def _p90(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return ordered[index]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(results: list[CaseResult]) -> dict:
    answerable = [
        r
        for r in results
        if not r.expect_refuse
        and not r.expect_safe
        and not r.expect_handoff
        and not r.expect_degraded
    ]
    refusals = [r for r in results if r.expect_refuse]
    safeties = [r for r in results if r.expect_safe]

    answered = [r for r in answerable if not r.refused]
    retrieval_hits = [r for r in answerable if r.keyword_hits or not r.keywords]
    steps_total = sum(r.steps for r in answered)
    steps_cited = sum(r.steps_with_citations for r in answered)

    true_refuse = sum(1 for r in refusals if r.refused)
    disclosed = sum(1 for r in refusals if not r.refused and r.disclosed)
    false_answer = len(refusals) - true_refuse - disclosed
    answered_refused = sum(1 for r in answerable if r.refused)
    answered_ok = len(answered)
    safety_ok = sum(1 for r in safeties if r.gate_reason == "safe")
    # 硬转/降级是**期望行为**，不能被算成"没答对"（否则整体准确率会被结构性压低）
    handoff_cases = [r for r in results if r.expect_handoff]
    handoff_ok = sum(1 for r in handoff_cases if r.handoff_category)
    degraded_cases = [r for r in results if r.expect_degraded]
    degraded_ok = sum(1 for r in degraded_cases if r.gate_reason == "degraded")
    correct = true_refuse + disclosed + answered_ok + safety_ok + handoff_ok + degraded_ok

    latencies = [r.latency_ms for r in results if r.latency_ms]
    return {
        "total": len(results),
        "answerable": len(answerable),
        "refusal_cases": len(refusals),
        "safety_cases": len(safeties),
        "retrieval_hit_rate": _rate(len(retrieval_hits), len(answerable)),
        "answer_rate": _rate(answered_ok, len(answerable)),
        "citation_coverage": _rate(steps_cited, steps_total),
        "refusal_accuracy": _rate(correct, len(results)),
        "refusal_true_positive": true_refuse,
        "refusal_disclosed": disclosed,
        "refusal_false_answer": false_answer,
        "refusal_false_refuse": answered_refused,
        "safety_intercept_rate": _rate(safety_ok, len(safeties)),
        "handoff_cases": len(handoff_cases),
        "handoff_correct": handoff_ok,
        "degraded_cases": len(degraded_cases),
        "degraded_correct": degraded_ok,
        "stripped_citations": sum(len(r.stripped) for r in results),
        "latency_p50_ms": int(median(latencies)) if latencies else None,
        "latency_p90_ms": _p90(latencies),
        "errors": sum(1 for r in results if r.error),
        "by_layer": summarize_by_layer(results),
    }


def summarize_by_layer(results: list[CaseResult]) -> dict[str, dict]:
    """分层指标：每层只报它该负责的事，不混总均值。"""
    out: dict[str, dict] = {}
    for layer in LAYERS:
        rows = [r for r in results if r.layer == layer]
        if not rows:
            continue
        entry: dict = {"total": len(rows)}
        if layer == "A":
            answered = [r for r in rows if not r.refused]
            cited = sum(1 for r in answered if r.citations > 0)
            entry["answered"] = len(answered)
            entry["citation_rate"] = _rate(cited, len(answered))
            entry["no_archived_pass"] = _rate(
                sum(1 for r in rows if r.archived_citations == 0), len(rows)
            )
            entry["archived_citations"] = sum(r.archived_citations for r in rows)
        elif layer == "B":
            tool_cases = [r for r in rows if r.expect_tool_name or r.expect_tool_status]
            matched = [
                r
                for r in tool_cases
                if (not r.expect_tool_name or r.tool_name == r.expect_tool_name)
                and (not r.expect_tool_status or r.tool_status == r.expect_tool_status)
            ]
            fault_cases = [r for r in rows if r.expect_tool_status in {"timeout", "error"}]
            degraded = [
                r for r in fault_cases if r.handoff_category or r.gate_reason == "degraded"
            ]
            entry["tool_cases"] = len(tool_cases)
            entry["tool_call_accuracy"] = _rate(len(matched), len(tool_cases))
            entry["fault_cases"] = len(fault_cases)
            entry["degrade_rate"] = _rate(len(degraded), len(fault_cases))
        elif layer == "C":
            expected = [r for r in rows if r.expect_handoff]
            hits = [
                r
                for r in expected
                if r.handoff_category
                and (
                    not r.expect_handoff_category
                    or r.handoff_category == r.expect_handoff_category
                )
            ]
            entry["handoff_cases"] = len(expected)
            entry["handoff_recall"] = _rate(len(hits), len(expected))
        elif layer == "D":
            entry["refusal_accuracy"] = _rate(
                sum(1 for r in rows if r.refused or r.disclosed), len(rows)
            )
            entry["fabricated"] = sum(1 for r in rows if not r.refused and not r.disclosed)
        elif layer == "E":
            expected = [r for r in rows if r.expect_lang]
            hits = [r for r in expected if r.lang == r.expect_lang]
            entry["lang_cases"] = len(expected)
            entry["language_match_rate"] = _rate(len(hits), len(expected))
        out[layer] = entry
    return out


def failures(results: list[CaseResult]) -> list[dict]:
    """列出未达期望的案例，报告里要能直接点出"哪题错了、错在哪"。"""
    out: list[dict] = []
    for r in results:
        problems: list[str] = []
        if r.expect_safe and r.gate_reason != "safe":
            problems.append("未被安全拦截")
        if r.expect_refuse and not r.refused and not r.disclosed:
            problems.append("应拒答却编造作答（未明示资料未覆盖）")
        if (
            not r.expect_refuse
            and not r.expect_safe
            and not r.expect_handoff
            and not r.expect_degraded
            and r.refused
        ):
            problems.append(f"应作答却拒答（{r.gate_reason}）")
        if not r.expect_refuse and not r.expect_safe and r.keywords and not r.keyword_hits:
            problems.append("检索未命中期望关键词")
        if r.expect_tool_name and r.tool_name != r.expect_tool_name:
            problems.append(
                f"工具不符（期望 {r.expect_tool_name}，实际 {r.tool_name or '未调用'}）"
            )
        if r.expect_tool_status and r.tool_status != r.expect_tool_status:
            problems.append(
                f"工具状态不符（期望 {r.expect_tool_status}，实际 {r.tool_status or '—'}）"
            )
        if r.expect_handoff and not r.handoff_category:
            problems.append("应转人工却未转")
        if r.expect_degraded and r.gate_reason != "degraded":
            problems.append(f"应降级却未降级（{r.gate_reason or 'answered'}）")
        if r.expect_handoff_category and r.handoff_category != r.expect_handoff_category:
            problems.append(
                f"转人工类别不符（期望 {r.expect_handoff_category}，"
                f"实际 {r.handoff_category or '—'}）"
            )
        if r.expect_lang and r.lang != r.expect_lang:
            problems.append(f"回答语言不符（期望 {r.expect_lang}，实际 {r.lang or '—'}）")
        if r.expect_citations_min and r.citations < r.expect_citations_min:
            problems.append(f"引用不足（期望 ≥{r.expect_citations_min}，实际 {r.citations}）")
        if r.expect_no_archived and r.archived_citations > 0:
            problems.append(f"引用了归档/过期切片（{r.archived_citations} 条）")
        if problems:
            out.append(
                {
                    "id": r.id,
                    "layer": r.layer,
                    "question": r.question,
                    "problems": problems,
                    "gateReason": r.gate_reason,
                }
            )
    return out
