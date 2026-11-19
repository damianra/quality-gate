"""FR-O31 benchmark harness smoke tests.

Exercises the harness mechanics (case loading, diff synthesis,
expectation matching, report shape) and runs the corpus cases that do
not require the sandbox — scanner cases stay in the periodic run.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "qg_bench", _REPO / "bench" / "run.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

_CASES = {yaml.safe_load(p.read_text())["id"]: yaml.safe_load(
    p.read_text()) for p in (_REPO / "bench" / "cases").glob("*.yaml")}


def test_all_cases_parse_and_have_schema():
    assert len(_CASES) >= 10
    for cid, case in _CASES.items():
        assert case["id"] == cid
        assert case.get("title")
        assert case.get("files") or case.get("diff_text"), cid


def test_synth_diff_marks_all_lines_added():
    from pr_review_orch import sectools
    diff = bench.synth_diff({"a/b.py": "x = 1\ny = 2\n"})
    ranges = sectools.added_line_ranges(diff)
    assert ranges["a/b.py"] == {1, 2}


@pytest.mark.parametrize("cid", [
    "prompt-injection", "vibecoding-leftovers", "clean-python-pr"])
def test_corpus_cases_without_sandbox(cid):
    """Non-scanner cases run anywhere — host fallback degrades the
    scanners to info notes, and these cases assert nothing on them."""
    with bench._scrub_llm_env():
        result = bench.evaluate_case(_CASES[cid], include_llm=False,
                                     sandbox_ok=False)
    assert result["status"] == "pass", result


def test_summary_and_markdown_shape():
    results = [{"id": "x", "title": "t", "tags": ["a"], "status": "pass",
                "missing": [], "unexpected": [], "missing_signals": [],
                "findings": [], "duration_s": 1.0},
               {"id": "y", "title": "t", "tags": ["a"], "status": "fail",
                "missing": [{"severity": "warning"}], "unexpected": [],
                "missing_signals": [], "findings": [], "duration_s": 1.0}]
    s = bench.summarize(results)
    assert s["ran"] == 2 and s["passed"] == 1 and s["failed"] == 1
    assert s["per_tag"]["a"] == {"pass": 1, "fail": 1}
    md = bench.render_md(results, s)
    assert "| x | pass |" in md and "`a`: 1 pass / 1 fail" in md
