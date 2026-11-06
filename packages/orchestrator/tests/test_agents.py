"""Specialist selection + deterministic findings + verdict synthesis."""

from pr_review_orch import agents
from pr_review_orch.graph import synthesizer_node


def _state(**kw):
    base = {
        "repo_path": "/nonexistent",
        "pr_number": 1,
        "signals": {},
        "files": [],
        "findings": [],
        "errors": [],
        "active_pgids": [],
        "dry_run": True,
        "head_sha": "abc1234deadbeef",
    }
    base.update(kw)
    return base


# -- router ------------------------------------------------------------------ #

def test_choose_agents_always_includes_review():
    assert agents.choose_agents({}, []) == ["review"]


def test_choose_agents_security_on_secrets():
    s = {"secret_candidates": ["api_key = 'x'"]}
    assert "security" in agents.choose_agents(s, [])


def test_choose_agents_qa_on_ts_files():
    files = [{"filename": "src/app.tsx"}]
    assert "qa" in agents.choose_agents({}, files)
    assert "qa" not in agents.choose_agents({}, [{"filename": "README.md"}])


# -- specialists --------------------------------------------------------------- #

def test_review_flags_out_of_scope_and_secrets_agnostic():
    state = _state(
        signals={
            "files_outside_hint": ["scripts/deploy.sh"],
            "whitespace_ratio": 0.9,
            "lines_changed": 100,
            "files_changed": 2,
            "todos_added": ["// TODO: x"],
            "commented_blocks": [{"file": "a.ts", "lines": 4, "sample": "// x"}],
            "debug_leftovers": ["console.log(1)"],
        }
    )
    findings = agents.review_specialist(state)["findings"]
    titles = {f["title"] for f in findings}
    assert "File outside declared scope" in titles
    assert "Whitespace-dominated diff" in titles
    assert "Commented-out code block" in titles


def test_security_blocks_on_secret():
    state = _state(signals={"secret_candidates": ["api_key = 'sk-1'"]})
    findings = agents.security_specialist(state)["findings"]
    assert any(f["severity"] == "blocker" for f in findings)


# -- synthesizer ----------------------------------------------------------------- #

def test_synthesizer_rejects_on_blocker():
    state = _state(findings=[
        {"agent": "security", "severity": "blocker", "title": "Secret", "detail": "x"},
        {"agent": "review", "severity": "info", "title": "Note", "detail": "y"},
    ])
    out = synthesizer_node(state)
    assert out["verdict"]["approve"] is False
    assert out["posted"]["action"] == "dry_run"  # nothing posted on dry-run


def test_synthesizer_approves_clean_run():
    state = _state(findings=[
        {"agent": "review", "severity": "info", "title": "ok", "detail": "fine"},
    ])
    out = synthesizer_node(state)
    assert out["verdict"]["approve"] is True


def test_synthesizer_rejects_on_internal_errors():
    state = _state(errors=["qa: exploded"])
    out = synthesizer_node(state)
    assert out["verdict"]["approve"] is False
