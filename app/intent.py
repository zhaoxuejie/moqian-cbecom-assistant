"""意图分流与槽位抽取（确定性规则优先，不调模型）。

为什么用规则而不是让模型先分类：
- 评测要能判定"该调工具调了没、该追问有没有追问"，规则可复现、可解释；
- 模型分类会多一次往返与一份不确定性，而这一层的判断依据本来就明确（单号格式、诉求词）；
- 真模糊时默认走 `policy`（检索），宁可多查一次知识库，也不要硬猜成"事实问题"去编答案。

意图与去向（P2 定，P3 接工具）：

| intent | 去向 |
|---|---|
| `high_risk` | 短路转人工（不检索、不调模型） |
| `order` / `logistics` | 有单号 → P3 工具；没单号 → 追问卡 `order_no` |
| `policy` | RAG 检索 + 证据门 + 引用校验 |

输出只包含"从文本/会话里能确知的东西"：没有单号就是没有，不做补全。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 订单号与物流单号：演示语料的确定格式（WB-10001 / 1Z + 16 位）
ORDER_NO_PATTERN = re.compile(r"\bWB-\d{5}\b", re.IGNORECASE)
TRACKING_NO_PATTERN = re.compile(r"\b1Z[0-9A-Z]{16}\b", re.IGNORECASE)

# 政策类线索词：命中即走检索（"refund policy" 这种要答，不能被高风险词表误拦）
POLICY_HINTS: tuple[str, ...] = (
    "return",
    "refund policy",
    "exchange",
    "shipping",
    "ship to",
    "delivery",
    "how long",
    "size",
    "sizing",
    "fit",
    "payment",
    "pay with",
    "discount",
    "promo",
    "coupon",
    "customs",
    "duty",
    "duties",
    "tax",
    "privacy",
    "gdpr",
    "warranty",
    "care instruction",
)

# 事实类线索词：订单状态 vs 物流轨迹
ORDER_HINTS: tuple[str, ...] = (
    "where is my order",
    "my order",
    "order status",
    "has my order shipped",
    "did my order ship",
    "cancel my order",
    "change my address",
    "modify my order",
    "update my address",
    "when will my order",
)
# 政策式问法：问"规则/一般情况"，不该因为提到 tracking/order 就索要单号
POLICY_QUESTION_HINTS: tuple[str, ...] = (
    "is that normal",
    "is this normal",
    "is it normal",
    "usually",
    "typically",
    "generally",
    "how long",
    "how many",
    "what is your policy",
    "policy",
)
LOGISTICS_HINTS: tuple[str, ...] = (
    "tracking",
    "track my",
    "track the",
    "where is my package",
    "where is my parcel",
    "delivery status",
    "in transit",
    "out for delivery",
    "when will it arrive",
    "when will it be delivered",
    "eta",
    "customs hold",
    "stuck",
)

# 语言检测：只做"明显能判断"的，判不出来就沿用会话 locale（再兜底 en）
LANGUAGE_HINTS: dict[str, tuple[str, ...]] = {
    "es": (
        "hola", "cuánto", "cuanto", "devolver", "reembolso",
        "envío", "envio", "talla", "gracias", "pedido",
    ),
    "fr": (
        "bonjour", "combien", "retour", "remboursement",
        "livraison", "taille", "merci", "commande",
    ),
    "de": (
        "hallo", "wie lange", "rückgabe", "erstattung",
        "versand", "größe", "danke", "bestellung",
    ),
}

# 国家识别：只在问题里出现明确国家名/代码时使用（会话里给的国家优先级更高）。
# 两位国家代码要求**大写**才算（避免 "us" 这种代词误命中），国家名不区分大小写。
COUNTRY_CODES: tuple[str, ...] = ("US", "UK", "DE", "FR", "ES")
COUNTRY_NAMES: dict[str, str] = {
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "america": "US",
    "united kingdom": "UK",
    "britain": "UK",
    "england": "UK",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "spain": "ES",
    "españa": "ES",
}

SUPPORTED_INTENTS = ("policy", "order", "logistics", "high_risk")


@dataclass(frozen=True)
class IntentDecision:
    """一次分流的结论。缺的字段一律 `None`，不猜。"""

    intent: str = "policy"
    order_no: str | None = None
    tracking_no: str | None = None
    country: str | None = None
    language: str = "en"
    confidence: float = 0.5
    reason: str = "default"
    missing: tuple[str, ...] = field(default_factory=tuple)

    def to_structured(self) -> dict:
        return {
            "intent": self.intent,
            "orderNo": self.order_no,
            "trackingNo": self.tracking_no,
            "country": self.country,
            "language": self.language,
            "missing": list(self.missing),
        }


def detect_language(question: str, *, fallback: str | None = None) -> str:
    lowered = (question or "").lower()
    for language, hints in LANGUAGE_HINTS.items():
        if any(hint in lowered for hint in hints):
            return language
    if fallback in {"en", "es", "fr", "de"}:
        return fallback
    return "en"


def detect_country(question: str) -> str | None:
    text = question or ""
    # 两位代码要求大写（"US" 而不是代词 "us"）
    code = re.search(r"\b(?:US|UK|DE|FR|ES)\b", text)
    if code:
        return code.group(0)
    lowered = text.lower()
    # 国家名先长后短，避免 "england" 之类被更短的别名截断
    for alias in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            return COUNTRY_NAMES[alias]
    return None


def match_high_risk(question: str) -> str | None:
    """高风险诉求命中：直接复用转人工规则引擎，避免两套词表各说各话。

    （历史上这里是独立词表；德语裸 "Erstattung" 曾被误当退款诉求，
    问"退款要多久"会被判高风险。规则引擎是唯一权威。）
    """
    from . import handoff_rules

    decision = handoff_rules.detect(question)
    return decision.matched if decision else None


def classify(
    question: str,
    *,
    session_country: str | None = None,
    session_locale: str | None = None,
) -> IntentDecision:
    text = question or ""
    order_no = ORDER_NO_PATTERN.search(text)
    tracking_no = TRACKING_NO_PATTERN.search(text)
    language = detect_language(text, fallback=session_locale)
    country = (session_country or "").strip().upper() or detect_country(text)
    order_value = order_no.group(0).upper() if order_no else None
    tracking_value = tracking_no.group(0).upper() if tracking_no else None

    risk_word = match_high_risk(text)
    if risk_word:
        return IntentDecision(
            intent="high_risk",
            order_no=order_value,
            tracking_no=tracking_value,
            country=country,
            language=language,
            confidence=0.95,
            reason=f"safety:{risk_word}",
        )

    lowered = text.casefold()
    # 政策式问法（"8 天没更新正常吗 / 一般多久"）即使提到 tracking/order，也不该索要单号：
    # 这类问题问的是规则，不是这一单的状态。
    policy_hint = any(hint in lowered for hint in POLICY_QUESTION_HINTS)
    order_hint = any(hint in lowered for hint in ORDER_HINTS)
    if policy_hint and not order_hint and not tracking_value and not order_value:
        return IntentDecision(
            intent="policy",
            order_no=None,
            tracking_no=None,
            country=country,
            language=language,
            confidence=0.7,
            reason="policy_question",
        )

    if tracking_value or any(hint in lowered for hint in LOGISTICS_HINTS):
        missing = () if (tracking_value or order_value) else ("order_no",)
        return IntentDecision(
            intent="logistics",
            order_no=order_value,
            tracking_no=tracking_value,
            country=country,
            language=language,
            confidence=0.85 if (tracking_value or order_value) else 0.7,
            reason="tracking" if tracking_value else "logistics_hint",
            missing=missing,
        )

    if any(hint in lowered for hint in ORDER_HINTS):
        missing = () if order_value else ("order_no",)
        return IntentDecision(
            intent="order",
            order_no=order_value,
            tracking_no=tracking_value,
            country=country,
            language=language,
            confidence=0.85 if order_value else 0.7,
            reason="order_hint",
            missing=missing,
        )

    # 有单号但没说是查订单还是查物流：按订单处理（工具层会返回订单+物流概览）
    if order_value:
        return IntentDecision(
            intent="order",
            order_no=order_value,
            tracking_no=tracking_value,
            country=country,
            language=language,
            confidence=0.75,
            reason="order_no_only",
        )

    if any(hint in lowered for hint in POLICY_HINTS):
        return IntentDecision(
            intent="policy",
            country=country,
            language=language,
            confidence=0.8,
            reason="policy_hint",
        )

    # 默认走知识库：宁可多查一次，也不要猜成事实问题
    return IntentDecision(
        intent="policy",
        country=country,
        language=language,
        confidence=0.5,
        reason="default",
    )
