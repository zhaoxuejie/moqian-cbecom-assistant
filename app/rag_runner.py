"""问答管线核心（S6 抽出来，供 `/chat` 与评测 runner **复用同一套代码**，ADR9）。

为什么单独一层：`/chat` 要的是"SSE 帧 + 落日志"，评测要的是"结果对象 + 指标"。
如果评测另写一遍检索/证据门/引用校验，就失去"评测反映线上"的意义。所以这里只做
**一次问答的计算**，不碰 SSE、不写 messages 表；事件与日志留在 `chat_service`。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg

from modules.evidence import GateResult, evaluate_gate
from modules.llm import LlmError
from modules.retrieval import RetrievalPipeline, RetrievalResult, ScoredChunk
from modules.tools.types import TOOL_ERROR, TOOL_TIMEOUT

from . import handoff_rules, retrieval_policy, tool_flow
from .config import Settings
from .domain import GAP_BODY
from .intent import classify
from .rag import RagStep, build_messages, build_retrieval_config, detect_conflict, parse_answer
from .runtime_config import load_config
from .schemas import ClarifySelection, HostContext

logger = logging.getLogger(__name__)


@dataclass
class TurnOutcome:
    question: str
    structured: dict[str, Any]
    filter_note: str | None = None
    safety_word: str | None = None
    retrieval: RetrievalResult | None = None
    gate: GateResult | None = None
    conflict: dict | None = None
    ask: dict | None = None
    # 转人工（P4）：四类硬转或连续失败
    handoff: dict[str, Any] | None = None
    steps: list[RagStep] = field(default_factory=list)
    citations: list[ScoredChunk] = field(default_factory=list)
    # 工具调用（P3）：事件载荷、原始结果、降级信息
    tool: dict[str, Any] | None = None
    tool_outcome: Any = None
    degraded: dict[str, Any] | None = None
    stripped: list[str] = field(default_factory=list)
    refused: bool = False
    gate_reason: str | None = None
    """`safe` / `gap` / `citation_stripped` / `error`；`None` 表示正常回答。"""
    content: str = ""
    error: Exception | None = None
    elapsed_ms: int = 0


def build_structured(
    question: str,
    *,
    context: HostContext | None = None,
    clarify: ClarifySelection | None = None,
) -> dict[str, Any]:
    """结构化条件：意图 + 槽位。缺什么就空着，不猜（纪律 2）。

    意图与槽位来自 `app.intent`（纯规则）；`clarify` 只用于"用户回答了追问卡"这一件事，
    而且只接受 `order_no` 一个字段——多字段追问会让对话变成表单。
    """
    decision = classify(
        question,
        session_country=context.country if context else None,
        session_locale=context.locale if context else None,
    )
    order_no = decision.order_no
    if clarify and clarify.field == "order_no":
        value = (clarify.value or "").strip()
        order_no = None if value.lower() in {"", "unknown", "skip"} else value.upper()
    missing = (
        ["order_no"] if decision.intent in {"order", "logistics"} and not order_no else []
    )
    return {**decision.to_structured(), "orderNo": order_no, "missing": missing}


def match_safety_word(question: str, words: tuple[str, ...]) -> str | None:
    """高风险词匹配（大小写不敏感；跨语言访客不会都大写）。"""
    lowered = (question or "").casefold()
    for word in words:
        if word and word.casefold() in lowered:
            return word
    return None


# 证据门覆盖率用的停用词：只剔除"问句骨架"，保留业务名词与数字
_COVERAGE_STOPWORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
        "can", "could", "would", "should", "i", "my", "me", "you", "your",
        "what", "how", "when", "where", "which", "who", "why",
        "to", "for", "of", "in", "on", "at", "it", "this", "that",
        "and", "or", "but", "if", "please", "hi", "hello", "thanks", "thank",
    }
)


def coverage_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if token not in _COVERAGE_STOPWORDS]


def build_order_ask(
    structured: dict[str, Any],
    clarify: ClarifySelection | None,
) -> dict[str, Any] | None:
    """订单/物流类问题缺单号时的单轮追问卡（一轮为限）。

    不给选项就没法作答：让访客要么给单号、要么明确"没有单号"转政策口径。
    选项值 `unknown` / `skip` 都表示"没有单号"，下一轮不再追问。
    """
    if clarify is not None:
        return None
    if structured.get("intent") not in {"order", "logistics"}:
        return None
    if structured.get("orderNo") or structured.get("trackingNo"):
        return None
    return {
        "field": "order_no",
        "title": "What's your order number?",
        "body": (
            "I need the order number to check its status — it looks like WB-10001 "
            "in your confirmation email. Without it I can only answer general policy "
            "questions."
        ),
        "options": [
            {
                "value": "unknown",
                "label": "I don't have the order number",
                "hint": "Answer from our policies instead",
            },
            {
                "value": "skip",
                "label": "Skip order lookup",
                "hint": "Continue without checking the order",
            },
        ],
    }


def run_turn(
    *,
    settings: Settings,
    conn: psycopg.Connection,
    llm: Any,
    embedder: Any,
    reranker: Any = None,
    question: str,
    context: HostContext | None = None,
    clarify: ClarifySelection | None = None,
    demo_fault: str | None = None,
    recent_gate_reasons: list[str | None] | None = None,
) -> TurnOutcome:
    """跑一轮完整问答，返回结果对象（不写日志、不发 SSE）。"""
    started = time.perf_counter()
    structured = build_structured(question, context=context, clarify=clarify)
    outcome = TurnOutcome(question=question, structured=structured)

    # 0. 运行时配置覆盖（检索 / 转人工）：保存后下一次问答即生效（D-012）；
    #    读取失败/无配置时 load_config 返回空，不影响主链路。
    runtime = load_config(conn, ("retrieval", "handoff"))
    retrieval_overrides = runtime.get("retrieval", {})
    handoff_overrides = runtime.get("handoff", {})

    # 1. 转人工规则（四类硬转 + 连续失败）：命中即短路——不检索、不调模型、不出 steps
    handoff = handoff_rules.detect(question, config=handoff_overrides)
    if handoff is None:
        handoff = handoff_rules.consecutive_failures(
            recent_gate_reasons, config=handoff_overrides
        )
    if handoff:
        outcome.handoff = handoff.to_event(
            hotline=settings.handoff_hotline, hours=settings.handoff_hours
        )
        outcome.refused = True
        outcome.gate_reason = "handoff"
        outcome.content = handoff.text
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # 1.5 条件不足的单轮追问：订单/物流类问题先要订单号，再检索/作答
    ask = build_order_ask(structured, clarify)
    if ask:
        outcome.ask = ask
        outcome.gate_reason = "ask"
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # 1.6 事实类问题：查 mock 订单/物流（**不调模型**，事实只来自工具返回值）。
    tool_plan = tool_flow.plan(structured)
    if tool_plan:
        outcome.tool = {
            "name": tool_plan.name,
            "status": "running",
            "args": {tool_plan.arg_name: tool_plan.value},
        }
        tool_outcome = tool_flow.invoke(
            conn,
            tool_plan,
            timeout_s=settings.tool_timeout_seconds,
            fault=demo_fault if settings.demo_fault_enabled else None,
        )
        outcome.tool_outcome = tool_outcome
        outcome.tool = tool_flow.event_payload(tool_outcome, tool_plan)
        answer = tool_flow.compose_answer(tool_outcome, tool_plan)
        outcome.content = answer
        if tool_outcome.status in {TOOL_TIMEOUT, TOOL_ERROR}:
            # 查不到就明说 + 转人工；绝不把"没查到"翻译成一个到达时间
            outcome.refused = True
            outcome.gate_reason = "degraded"
            outcome.degraded = {
                "reason": tool_outcome.status,
                "text": answer,
                "handoffRequired": True,
            }
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        outcome.steps = [RagStep(index=1, text=answer, citations=[])]
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # 2. 检索
    try:
        pipeline = RetrievalPipeline(
            settings=build_retrieval_config(settings, retrieval_overrides),
            embedder=embedder,
            reranker=reranker,
        )
        retrieval = pipeline.retrieve(
            conn,
            question=question,
            model=None,
            batch=None,
            model_aliases=(),
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成 error 结果
        logger.exception("retrieval failed: %s", exc)
        outcome.error = exc
        outcome.refused = True
        outcome.gate_reason = "error"
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome
    outcome.retrieval = retrieval
    # 模块层的 filterNote 带"型号/批次"行业措辞（公共层不写死行业词的要求没覆盖到），
    # 这里只采用 app 层后置过滤产生的说明；没有过滤就留空，不把行业文案透给访客。
    outcome.filter_note = None

    # 2.5 后置过滤：按适用国家与生效日期收敛证据集（行业语义在 app 层）。
    #     必须在证据门之前做：门控的 hits / top_score / coverage 要按"过滤后"重算，
    #     否则会出现"命中数够、但适用国家的政策全被滤掉"却仍然作答。
    policy = retrieval_policy.apply(
        conn, retrieval.items, country=(context.country if context else None)
    )
    if policy.dropped_count:
        retrieval.items = policy.kept
    if policy.note:
        outcome.filter_note = policy.note

    # 3. 证据门（纪律 4）
    try:
        min_score = float(
            retrieval_overrides.get("evidenceThreshold", settings.retrieval_min_score)
        )
    except (TypeError, ValueError):
        min_score = settings.retrieval_min_score
    try:
        min_coverage = float(
            retrieval_overrides.get("evidenceMinCoverage", settings.evidence_min_coverage)
        )
    except (TypeError, ValueError):
        min_coverage = settings.evidence_min_coverage
    # 跨语种检索时，问题里的词不会出现在英文证据里，覆盖率天然偏低：
    # 对非英语提问放宽覆盖率（仍要求命中数与分数达标），否则德语问题会一律判 gap。
    # 0.10 是实测值：德语退款问题覆盖率约 0.125–0.14，0.15 会误杀。
    language = str(structured.get("language") or "en")
    effective_coverage = min_coverage if language == "en" else min(min_coverage, 0.10)
    gate = evaluate_gate(
        retrieval.items,
        query_tokens=coverage_tokens(retrieval.query_tokens),
        min_hits=settings.evidence_min_hits,
        min_score=min_score,
        min_coverage=effective_coverage,
    )
    outcome.gate = gate
    if not gate.passed:
        outcome.refused = True
        outcome.gate_reason = "gap"
        outcome.content = GAP_BODY
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # 4. 多版本冲突（纪律 7）：必须按同一份文档比较，先取 docId 再判断
    conflict_docs = retrieval_policy.doc_codes(
        conn, [item.evidence_id for item in retrieval.items[:3]]
    )
    outcome.conflict = detect_conflict(retrieval.items, doc_codes=conflict_docs)

    # 5. 生成（temperature=0：同一证据要可复现；空内容加倍预算重试一次）
    messages_list = build_messages(question, retrieval, structured)
    budget = settings.llm_answer_max_tokens
    try:
        try:
            result = llm.complete(messages_list, max_tokens=budget, temperature=0.0)
        except LlmError as exc:
            if exc.code != "LLM_EMPTY_CONTENT":
                raise
            logger.warning("empty content, retry with larger budget: %d -> %d", budget, budget * 2)
            result = llm.complete(messages_list, max_tokens=budget * 2, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline llm failed: %s", exc)
        outcome.error = exc
        outcome.refused = True
        outcome.gate_reason = "error"
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # 6. 引用校验（纪律 1）
    parsed = parse_answer(result.text, allowed_items=retrieval.items)
    outcome.steps = parsed.steps
    outcome.citations = parsed.citations
    outcome.stripped = parsed.stripped
    if parsed.refused:
        outcome.refused = True
        outcome.gate_reason = "citation_stripped"
        outcome.content = parsed.steps[0].text if parsed.steps else ""
    else:
        outcome.content = "\n".join(step.text for step in parsed.steps)
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return outcome
