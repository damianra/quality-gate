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


# -- LLM enrichment (FR-O16) ------------------------------------------------- #


def _fake_cfg(monkeypatch, **meta):
    """Patch for_role -> enabled test cfg; complete_with_meta -> meta/raise."""
    cfg = agents.llm.LLMConfig(
        enabled=True, provider="test", model="m1", role="review"
    )
    if isinstance(meta.get("exc"), Exception):
        monkeypatch.setattr(
            cfg, "complete_with_meta",
            lambda *a, **k: (_ for _ in ()).throw(meta["exc"]),
        )
    else:
        monkeypatch.setattr(
            cfg, "complete_with_meta", lambda *a, **k: meta["meta"]
        )
    monkeypatch.setattr(
        agents.llm.LLMConfig, "for_role",
        classmethod(lambda cls, role, env=None: cfg),
    )
    return cfg


def test_enrich_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(
        agents.llm.LLMConfig, "for_role",
        classmethod(lambda cls, role, env=None: agents.llm.LLMConfig()),
    )
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    assert out == {"findings": [], "llm_calls": [], "active_pgids": []}


def test_enrich_config_error_degrades(monkeypatch):
    def boom(cls, role, env=None):
        raise agents.llm.LLMConfigError("missing key")
    monkeypatch.setattr(agents.llm.LLMConfig, "for_role", classmethod(boom))
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    assert out["findings"][0]["title"] == "LLM enrichment unavailable"
    assert out["llm_calls"] == []


def test_enrich_ok_merges_findings_and_records_call(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": 'preamble [{"severity": "blocker", "title": "Leaked key",'
                ' "detail": "api key in diff", "file": "a.js"}] tail',
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "finish_reason": "stop", "truncated": False, "duration_s": 1.5,
    })
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    blocker = [f for f in out["findings"] if f["severity"] == "blocker"]
    assert blocker and blocker[0]["title"] == "LLM: Leaked key"
    assert blocker[0]["file"] == "a.js"
    call = out["llm_calls"][0]
    assert (call["role"], call["node"], call["status"]) == (
        "review", "review", "ok")
    assert call["provider"] == "test" and call["model"] == "m1"
    assert call["usage"]["prompt_tokens"] == 10
    assert out["active_pgids"] == []


def test_enrich_call_error_never_rejects(monkeypatch):
    _fake_cfg(monkeypatch, exc=RuntimeError("devin-cli failed"))
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    titles = {f["title"] for f in out["findings"]}
    assert "LLM enrichment unavailable" in titles
    assert all(f["severity"] == "info" for f in out["findings"])
    assert out["llm_calls"][0]["status"] == "error"


def test_enrich_truncated_degrades(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": "[{", "usage": {"prompt_tokens": 1, "completion_tokens": 9},
        "finish_reason": "length", "truncated": True, "duration_s": 0.4,
    })
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    assert out["findings"][0]["title"] == "LLM enrichment truncated"
    assert out["llm_calls"][0]["status"] == "truncated"


def test_enrich_unparseable_degrades(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": "I cannot review this.", "usage": {},
        "finish_reason": "stop", "truncated": False, "duration_s": 0.2,
    })
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    assert out["findings"][0]["title"] == "LLM enrichment unparseable"
    assert out["llm_calls"][0]["status"] == "unparseable"


def test_enrich_registers_subprocess_pgid(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": "[]", "usage": {}, "finish_reason": None,
        "truncated": False, "duration_s": 2.0, "pgid": 4242,
    })
    out = agents._llm_enrich("review", agents.REVIEW_PROMPT, _state())
    assert out["active_pgids"] == [4242]


def test_llm_findings_clamps_bad_severity_and_caps():
    import json as _json
    items = [{"severity": "critical", "title": "t", "detail": "d"}] * 15
    out = agents._llm_findings("security", _json.dumps(items))
    assert len(out) == agents._LLM_MAX_FINDINGS
    assert all(f["severity"] == "info" for f in out)
    assert all(f["title"].startswith("LLM: ") for f in out)


# -- wiring ------------------------------------------------------------------- #


def test_review_specialist_merges_llm_enrichment(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": '[{"severity": "warning", "title": "Scope smell",'
                ' "detail": "diff touches unrelated dirs", "file": null}]',
        "usage": {"prompt_tokens": 5, "completion_tokens": 6},
        "finish_reason": "stop", "truncated": False, "duration_s": 0.9,
    })
    out = agents.review_specialist(_state(
        signals={"debug_leftovers": ["console.log(1)"]}))
    titles = {f["title"] for f in out["findings"]}
    assert "Debug leftovers" in titles        # deterministic pass kept
    assert "LLM: Scope smell" in titles       # enrichment merged
    assert out["llm_calls"][0]["role"] == "review"
    assert out["active_pgids"] == []


def test_review_specialist_deterministic_when_llm_disabled(monkeypatch):
    monkeypatch.setattr(
        agents.llm.LLMConfig, "for_role",
        classmethod(lambda cls, role, env=None: agents.llm.LLMConfig()),
    )
    out = agents.review_specialist(_state())
    assert out["llm_calls"] == []
    assert out["active_pgids"] == []
