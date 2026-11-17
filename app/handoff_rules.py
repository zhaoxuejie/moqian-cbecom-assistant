"""转人工规则引擎（四类硬转 + 连续失败辅助触发）。

为什么独立成文件而不是塞进 `domain`：
- 它是有状态的规则（后台开关、阈值、话术可配），`domain` 只放静态词汇；
- 评测要能对"哪一类、命中哪条规则"做断言，所以返回结构化结果，不是一句布尔。

四类（PRD §4.2，无条件硬转，不看情绪）：
`refund` 退款诉求 / `complaint` 投诉差评 / `legal` 法务 / `account` 账户安全。

关键取舍：**只匹配"表达诉求"的短语**。裸 `refund` 会把 "What is your refund policy?"
这种正常政策问答也拦下来——那不是谨慎，是产品坏了。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CATEGORIES: tuple[str, ...] = ("refund", "complaint", "legal", "account")

# 失败类 gate_reason：连续出现达到阈值 → 强制转人工
FAILURE_REASONS: frozenset[str] = frozenset({"gap", "citation_stripped", "degraded"})

# 每类短语（英/西/法/德 + 少量中文便于内部演示）。一律小写匹配。
CATEGORY_PHRASES: dict[str, tuple[str, ...]] = {
    "refund": (
        "want a refund", "need a refund", "refund me", "give me a refund",
        "refund my order", "want my money", "money back", "chargeback", "charge back",
        "paypal dispute",
        "quiero un reembolso", "reembolsad", "devuélveme", "devolverme el dinero",
        "je veux un remboursement", "remboursez", "rendez-moi mon argent",
        "ich will mein geld zurück", "geld zurück",
        "退款", "退钱", "把钱退",
    ),
    "complaint": (
        "file a complaint", "make a complaint", "complain to", "bad review",
        "leave a review", "negative review", "trustpilot", "1-star", "one star review",
        "post about this", "social media",
        "queja", "reclamación", "voy a escribir una reseña",
        "réclamation", "avis négatif", "je vais laisser un avis",
        "beschwerde", "schlechte bewertung",
        "投诉", "差评",
    ),
    "legal": (
        "lawyer", "attorney", "sue you", "lawsuit", "legal action", "take legal",
        "press charges", "fraud", "scam",
        "abogado", "demanda", "fraude", "estafa",
        "avocat", "poursuite", "arnaque",
        "anwalt", "klage", "betrug",
        "起诉", "律师", "欺诈",
    ),
    "account": (
        "hacked", "account was stolen", "account stolen", "unauthorized charge",
        "unauthorised charge", "charge i didn't make", "someone used my card",
        "account security", "my account is locked",
        "hackearon", "cargo no autorizado", "cuenta bloqueada",
        "compte piraté", "paiement non autorisé", "compte bloqué",
        "konto gehackt", "unbefugte abbuchung", "konto gesperrt",
        "账户被盗", "未经授权", "扣款不对",
    ),
}

# 写入类诉求：我们**不提供工具**，只能引导 + 转人工（按退款类处理）
WRITE_REQUEST_PHRASES: tuple[str, ...] = (
    "cancel my order", "cancel the order", "change my address", "change the address",
    "update my address", "modify my order", "cancel order",
    "cancelar mi pedido", "cambiar mi dirección",
    "annuler ma commande", "changer mon adresse",
    "bestellung stornieren", "adresse ändern",
    "取消订单", "改地址",
)

DEFAULT_TEXTS: dict[str, str] = {
    "refund": (
        "Refunds and money-back requests are handled by a human agent — I can't approve or "
        "promise any amount. I'm handing this to our support team (support@wanderbrim.com, "
        "Mon–Fri 9:00–17:00 MST)."
    ),
    "complaint": (
        "I'm sorry this happened. Complaints and review-related issues go to our support team "
        "so they can be handled properly — I've handed this over (support@wanderbrim.com)."
    ),
    "legal": (
        "Legal matters must be handled by our support team, not by this assistant. "
        "I've handed the conversation over (support@wanderbrim.com)."
    ),
    "account": (
        "Account security and unauthorized charges need a human agent who can check your "
        "account. I've handed this over (support@wanderbrim.com)."
    ),
}

DEFAULT_THRESHOLD = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": {category: True for category in CATEGORIES},
    "texts": dict(DEFAULT_TEXTS),
    "threshold": DEFAULT_THRESHOLD,
    "continueAssist": True,
}

RULE_KEYWORD = "keyword"
RULE_WRITE_REQUEST = "write_request"
RULE_CONSECUTIVE = "consecutive_failures"


@dataclass(frozen=True)
class HandoffDecision:
    category: str
    rule: str
    matched: str
    text: str

    def to_event(self, *, hotline: str, hours: str) -> dict[str, Any]:
        return {
            "category": self.category,
            "rule": self.rule,
            "matched": self.matched,
            "text": self.text,
            "hotline": hotline,
            "hours": hours,
        }


def load_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """合并默认值；`app_config.handoff` 只覆盖它写了的键，缺的用默认。"""
    config = {
        "enabled": dict(DEFAULT_CONFIG["enabled"]),
        "texts": dict(DEFAULT_CONFIG["texts"]),
        "threshold": DEFAULT_CONFIG["threshold"],
        "continueAssist": DEFAULT_CONFIG["continueAssist"],
    }
    if not isinstance(raw, dict):
        return config
    enabled = raw.get("enabled")
    if isinstance(enabled, dict):
        for category in CATEGORIES:
            if category in enabled:
                config["enabled"][category] = bool(enabled[category])
    texts = raw.get("texts")
    if isinstance(texts, dict):
        for category in CATEGORIES:
            value = texts.get(category)
            if isinstance(value, str) and value.strip():
                config["texts"][category] = value.strip()
    threshold = raw.get("threshold")
    if isinstance(threshold, int) and threshold >= 1:
        config["threshold"] = threshold
    if isinstance(raw.get("continueAssist"), bool):
        config["continueAssist"] = raw["continueAssist"]
    return config


def _match_category(question: str, category: str) -> str | None:
    lowered = (question or "").casefold()
    for phrase in CATEGORY_PHRASES[category]:
        if phrase.casefold() in lowered:
            return phrase
    return None


def detect(question: str, *, config: dict[str, Any] | None = None) -> HandoffDecision | None:
    """只看这一句话有没有四类硬转诉求；返回第一命中（按 CATEGORIES 顺序）。"""
    settings = load_config(config)
    text = question or ""
    for category in CATEGORIES:
        if not settings["enabled"].get(category, True):
            continue
        matched = _match_category(text, category)
        if matched:
            return HandoffDecision(
                category=category,
                rule=RULE_KEYWORD,
                matched=matched,
                text=settings["texts"][category],
            )

    lowered = text.casefold()
    if settings["enabled"].get("refund", True):
        for phrase in WRITE_REQUEST_PHRASES:
            if phrase.casefold() in lowered:
                return HandoffDecision(
                    category="refund",
                    rule=RULE_WRITE_REQUEST,
                    matched=phrase,
                    text=settings["texts"]["refund"],
                )
    return None


def consecutive_failures(
    recent_gate_reasons: list[str | None] | None,
    *,
    config: dict[str, Any] | None = None,
) -> HandoffDecision | None:
    """最近连续失败的轮数达到阈值 → 转人工（只看尾部连续段，中间成功一次就清零）。"""
    settings = load_config(config)
    count = 0
    for reason in reversed(recent_gate_reasons or []):
        if reason in FAILURE_REASONS:
            count += 1
        else:
            break
    if count < settings["threshold"]:
        return None
    return HandoffDecision(
        category="refund",
        rule=RULE_CONSECUTIVE,
        matched=f"{count} consecutive failed turns",
        text=(
            "I haven't been able to give you a reliable answer twice in a row, so I'm "
            "handing this to a human agent rather than guessing. "
            "(support@wanderbrim.com, Mon–Fri 9:00–17:00 MST)"
        ),
    )
