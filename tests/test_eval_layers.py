"""评测分层扩展：schema 校验、dry-run、分层指标与失败判定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import runner
from evals.metrics import CaseResult, failures, summarize, summarize_by_layer
from evals.schema import EvalCaseError, count_by_layer, load_cases


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cases.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _case(id_: str, layer: str, **overrides) -> CaseResult:
    base = dict(
        id=id_,
        question="q",
        expect_refuse=False,
        expect_safe=False,
        refused=False,
        gate_reason=None,
        citations=1,
        steps=1,
        steps_with_citations=1,
        stripped=[],
        latency_ms=10,
        keywords=[],
        keyword_hits=[],
        layer=layer,
    )
    base.update(overrides)
    return CaseResult(**base)


def test_schema_parses_layered_case(tmp_path) -> None:
    path = _write(
        tmp_path,
        """
- id: A-001
  layer: A
  question: How long is the US return window?
  context: {country: US, locale: en}
  expect:
    keywords: ["30 days"]
    citationsMin: 1
    noArchived: true
- id: B-001
  layer: B
  question: Where is my order WB-10001?
  fixture: {fault: tool_timeout}
  expect:
    tool: {name: query_order, status: timeout}
    handoff: {required: true, category: refund}
- id: E-001
  layer: E
  question: ¿Cuánto dura el plazo?
  expect: {lang: es}
""",
    )
    cases = load_cases(path)
    assert count_by_layer(cases) == {"A": 1, "B": 1, "C": 0, "D": 0, "E": 1}
    assert cases[0].expect_no_archived is True
    assert cases[0].expect_citations_min == 1
    assert cases[1].expect_tool_name == "query_order"
    assert cases[1].expect_tool_status == "timeout"
    assert cases[1].fixture["fault"] == "tool_timeout"
    assert cases[2].expect_lang == "es"


def test_schema_rejects_unknown_layer(tmp_path) -> None:
    path = _write(tmp_path, "- id: X-1\n  layer: F\n  question: q\n")
    with pytest.raises(EvalCaseError, match="layer"):
        load_cases(path)


def test_schema_rejects_unknown_fault(tmp_path) -> None:
    path = _write(
        tmp_path,
        "- id: B-1\n  layer: B\n  question: q\n  fixture: {fault: boom}\n",
    )
    with pytest.raises(EvalCaseError, match="fault"):
        load_cases(path)


def test_dry_run_loads_without_model(tmp_path, capsys) -> None:
    path = _write(
        tmp_path,
        """
- id: A-1
  layer: A
  question: q1
- id: B-1
  layer: B
  question: q2
""",
    )
    code = runner.main(["--cases", str(path), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"cases": 2' in out


def test_dry_run_layer_filter(tmp_path, capsys) -> None:
    path = _write(
        tmp_path,
        "- id: A-1\n  layer: A\n  question: q1\n- id: B-1\n  layer: B\n  question: q2\n",
    )
    assert runner.main(["--cases", str(path), "--dry-run", "--layer", "B"]) == 0
    assert '"cases": 1' in capsys.readouterr().out


def test_layer_metrics_tool_and_handoff() -> None:
    results = [
        _case("B-1", "B", tool_name="query_order", tool_status="ok",
              expect_tool_name="query_order", expect_tool_status="ok"),
        _case("B-2", "B", tool_name="query_order", tool_status="timeout",
              expect_tool_name="query_order", expect_tool_status="timeout",
              gate_reason="degraded", handoff_category="refund"),
        _case("C-1", "C", expect_handoff=True, expect_handoff_category="refund",
              handoff_category="refund"),
        _case("C-2", "C", expect_handoff=True, expect_handoff_category="legal"),
    ]
    by_layer = summarize_by_layer(results)
    assert by_layer["B"]["tool_call_accuracy"] == 1.0
    assert by_layer["B"]["degrade_rate"] == 1.0
    assert by_layer["C"]["handoff_recall"] == 0.5

    problems = {item["id"]: item["problems"] for item in failures(results)}
    assert "C-2" in problems
    assert any("应转人工" in p for p in problems["C-2"])


def test_layer_metrics_archived_and_language() -> None:
    results = [
        _case("A-1", "A", citations=2, expect_citations_min=1),
        _case("A-2", "A", citations=1, archived_citations=1, expect_no_archived=True),
        _case("E-1", "E", lang="es", expect_lang="es"),
        _case("E-2", "E", lang="en", expect_lang="de"),
    ]
    by_layer = summarize_by_layer(results)
    assert by_layer["A"]["no_archived_pass"] == 0.5
    assert by_layer["A"]["archived_citations"] == 1
    assert by_layer["E"]["language_match_rate"] == 0.5

    problems = {item["id"]: item["problems"] for item in failures(results)}
    assert any("归档" in p for p in problems["A-2"])
    assert any("语言不符" in p for p in problems["E-2"])


def test_summary_includes_by_layer() -> None:
    summary = summarize([_case("A-1", "A")])
    assert "by_layer" in summary
    assert summary["by_layer"]["A"]["total"] == 1


def test_handoff_and_degraded_count_as_correct() -> None:
    """期望硬转/降级的用例必须算"符合期望"，否则整体准确率被结构性压低。"""
    results = [
        _case("C-1", "C", expect_handoff=True, handoff_category="refund",
              refused=True, gate_reason="handoff"),
        _case("B-1", "B", expect_degraded=True, refused=True, gate_reason="degraded"),
    ]
    summary = summarize(results)
    assert summary["handoff_correct"] == 1
    assert summary["degraded_correct"] == 1
    assert summary["refusal_accuracy"] == 1.0
