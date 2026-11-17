"""转人工规则引擎（纯函数）：四类硬转、写入类诉求、连续失败阈值。"""

from __future__ import annotations

from app.handoff_rules import (
    RULE_CONSECUTIVE,
    RULE_KEYWORD,
    RULE_WRITE_REQUEST,
    consecutive_failures,
    detect,
    load_config,
)


def test_refund_demand_triggers_refund() -> None:
    decision = detect("I want a refund for my order")
    assert decision is not None
    assert decision.category == "refund"
    assert decision.rule == RULE_KEYWORD


def test_refund_policy_question_does_not_trigger() -> None:
    """裸 refund 不算：问政策必须能正常答。"""
    assert detect("What is your refund policy?") is None
    assert detect("How long does a refund take after you receive the item?") is None


def test_complaint_threat_triggers() -> None:
    decision = detect("I'm going to leave a bad review on Trustpilot")
    assert decision is not None and decision.category == "complaint"


def test_legal_triggers() -> None:
    decision = detect("My lawyer will file a lawsuit")
    assert decision is not None and decision.category == "legal"


def test_account_security_triggers() -> None:
    decision = detect("I think my account was hacked and there is an unauthorized charge")
    assert decision is not None and decision.category == "account"


def test_spanish_refund_triggers() -> None:
    decision = detect("Quiero un reembolso de mi pedido")
    assert decision is not None and decision.category == "refund"


def test_french_complaint_triggers() -> None:
    decision = detect("Je vais laisser un avis négatif")
    assert decision is not None and decision.category == "complaint"


def test_german_legal_triggers() -> None:
    decision = detect("Ich hole meinen Anwalt, das ist Betrug")
    assert decision is not None and decision.category == "legal"


def test_german_refund_timing_question_does_not_trigger() -> None:
    """问"退款要多久"是政策问题，不是索要退款——裸 Erstattung 不能当诉求。"""
    assert detect("Wie lange dauert die Rückerstattung nach der Rücksendung?") is None


def test_write_request_maps_to_refund_without_tool() -> None:
    decision = detect("Please cancel my order")
    assert decision is not None
    assert decision.category == "refund"
    assert decision.rule == RULE_WRITE_REQUEST


def test_disabled_category_is_not_triggered() -> None:
    config = {"enabled": {"refund": False}}
    assert detect("I want a refund", config=config) is None
    # 其他类别不受影响
    assert detect("My lawyer will sue you", config=config) is not None


def test_consecutive_failures_threshold() -> None:
    assert consecutive_failures(["gap"]) is None
    decision = consecutive_failures(["gap", "citation_stripped"])
    assert decision is not None and decision.rule == RULE_CONSECUTIVE


def test_consecutive_resets_after_success() -> None:
    """中间成功一次就清零，不把昨天的失败累计到今天。"""
    assert consecutive_failures(["gap", None, "gap"]) is None


def test_threshold_is_configurable() -> None:
    config = {"threshold": 3}
    assert consecutive_failures(["gap", "gap"], config=config) is None
    assert consecutive_failures(["gap", "gap", "gap"], config=config) is not None


def test_custom_text_overrides_default() -> None:
    config = {"texts": {"refund": "Custom refund handoff text."}}
    decision = detect("I want a refund", config=config)
    assert decision is not None and decision.text == "Custom refund handoff text."


def test_load_config_ignores_bad_values() -> None:
    settings = load_config({"threshold": 0, "enabled": {"refund": "yes"}, "texts": {"refund": ""}})
    assert settings["threshold"] == 2, "非法阈值回退默认值"
    assert settings["texts"]["refund"], "空话术不应覆盖默认"
    assert settings["enabled"]["refund"] is True


def test_event_payload_shape() -> None:
    decision = detect("I want a refund")
    assert decision is not None
    event = decision.to_event(hotline="support@x", hours="9-17")
    assert set(event) == {
        "category",
        "rule",
        "matched",
        "text",
        "hotline",
        "hours",
    }
