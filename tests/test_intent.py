"""意图分流与槽位抽取（纯规则，不调模型）。"""

from __future__ import annotations

from app.intent import classify, detect_country, detect_language


def test_policy_question_routes_to_policy() -> None:
    decision = classify("How long is the US return window?")
    assert decision.intent == "policy"
    assert decision.country == "US"


def test_refund_policy_is_not_high_risk() -> None:
    """裸 refund 不算高风险：问政策要能答，只有表达诉求才转人工。"""
    decision = classify("What is your refund policy?")
    assert decision.intent == "policy"


def test_refund_demand_is_high_risk() -> None:
    decision = classify("I want a refund for my order WB-10001")
    assert decision.intent == "high_risk"
    assert decision.order_no == "WB-10001"


def test_legal_threat_is_high_risk() -> None:
    assert classify("I will sue you and my lawyer will call").intent == "high_risk"


def test_order_question_without_number_needs_clarify() -> None:
    decision = classify("Where is my order?")
    assert decision.intent == "order"
    assert decision.order_no is None
    assert decision.missing == ("order_no",)


def test_order_question_with_number_has_slot() -> None:
    decision = classify("Where is my order wb-10007?")
    assert decision.intent == "order"
    assert decision.order_no == "WB-10007"
    assert decision.missing == ()


def test_tracking_number_routes_to_logistics() -> None:
    decision = classify("My parcel is stuck, tracking 1z999aa10123456784")
    assert decision.intent == "logistics"
    assert decision.tracking_no == "1Z999AA10123456784"


def test_logistics_hint_without_number_needs_clarify() -> None:
    decision = classify("Can you track my package for me?")
    assert decision.intent == "logistics"
    assert decision.missing == ("order_no",)


def test_bare_order_number_treated_as_order() -> None:
    decision = classify("WB-10003")
    assert decision.intent == "order"
    assert decision.order_no == "WB-10003"


def test_language_detection_spanish() -> None:
    assert detect_language("Hola, ¿cuánto tarda el envío a España?") == "es"


def test_language_falls_back_to_session_locale() -> None:
    assert detect_language("ok", fallback="de") == "de"


def test_country_detection_from_question() -> None:
    assert detect_country("Do you ship to Germany?") == "DE"
    # 会话里给的国家优先，且短代码不会误命中普通单词（delivery / refund）
    decision = classify(
        "Do you ship to Germany?", session_country="US", session_locale="en"
    )
    assert decision.country == "US"


def test_unknown_question_defaults_to_policy() -> None:
    decision = classify("Tell me something interesting")
    assert decision.intent == "policy"
    assert decision.reason == "default"


def test_structured_shape_is_camel_case() -> None:
    payload = classify("Where is my order WB-10001?").to_structured()
    assert set(payload) == {
        "intent",
        "orderNo",
        "trackingNo",
        "country",
        "language",
        "missing",
    }


def test_policy_question_with_tracking_word_does_not_ask_order_no() -> None:
    decision = classify("My tracking hasn't updated for 8 days - is that normal?")
    assert decision.intent == "policy"
    assert decision.missing == ()
