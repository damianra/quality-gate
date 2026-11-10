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
    s = {"secret_candidates": [{"file": "a.ts", "line": "api_key = 'x'"}]}
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
    state = _state(signals={
        "secret_candidates": [{"file": "src/keys.ts", "line": "api_key = 'sk-1'"}]
    })
    findings = agents.security_specialist(state)["findings"]
    assert any(f["severity"] == "blocker" for f in findings)


def test_secret_in_test_file_is_warning_not_blocker():
    """Fixtures like token: 't' inside tests/ must not reject a PR."""
    state = _state(signals={
        "secret_candidates": [
            {"file": "tests/finalizer.test.js",
             "line": "const r = await fn(git, HTTPS, { token: 't' })"},
            {"file": "src/keys.ts", "line": "api_key = 'sk-real'"},
        ]
    })
    findings = agents.security_specialist(state)["findings"]
    # Only deterministic candidates — LLM findings may also cite the file.
    det = {
        f["file"]: f for f in findings
        if f["title"].startswith("Possible hardcoded secret")
    }
    assert det["tests/finalizer.test.js"]["severity"] == "warning"
    assert "(test file)" in det["tests/finalizer.test.js"]["title"]
    assert det["src/keys.ts"]["severity"] == "blocker"


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
    def _for_role(cls, role, env=None):
        cfg.role = role  # mirrors production: for_role stamps the role
        return cfg

    monkeypatch.setattr(
        agents.llm.LLMConfig, "for_role", classmethod(_for_role),
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


def test_security_specialist_merges_llm_enrichment(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": '[{"severity": "blocker", "title": "Rotated token leaked",'
                ' "detail": "old key still valid", "file": ".env"}]',
        "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        "finish_reason": "stop", "truncated": False, "duration_s": 1.1,
    })
    out = agents.security_specialist(_state(
        signals={"secret_candidates": [{"file": "a.ts", "line": "ghp_x"}]}))
    blockers = [f for f in out["findings"] if f["severity"] == "blocker"]
    assert any(f["title"] == "LLM: Rotated token leaked" for f in blockers)
    assert any(f["title"] == "Possible hardcoded secret" for f in blockers)
    assert out["llm_calls"][0]["node"] == "security"


def test_enrich_parses_findings_after_think_block(monkeypatch):
    """Reasoning-model output: <think> wrapper must not sink the JSON."""
    _fake_cfg(monkeypatch, meta={
        "text": '<think>let me analyze the diff for secrets\n</think>\n'
                '[{"severity": "blocker", "title": "Leaked key",'
                ' "detail": "x", "file": "src/keys.ts"}]',
        "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        "finish_reason": "stop", "truncated": False, "duration_s": 1.1,
    })
    out = agents.security_specialist(_state(
        signals={"secret_candidates": [{"file": "a.ts", "line": "ghp_x"}]}))
    blockers = [f for f in out["findings"] if f["severity"] == "blocker"]
    assert any(f["title"] == "LLM: Leaked key" for f in blockers)
    assert out["llm_calls"][0]["status"] == "ok"


def test_qa_specialist_merges_llm_enrichment(monkeypatch, tmp_path):
    _fake_cfg(monkeypatch, meta={
        "text": '[{"severity": "warning", "title": "Untested edge case",'
                ' "detail": "rollback path has no test", "file": null}]',
        "usage": {"prompt_tokens": 11, "completion_tokens": 13},
        "finish_reason": "stop", "truncated": False, "duration_s": 0.7,
    })
    # No files -> no validators -> early path; enrichment must still run.
    out = agents.qa_specialist(_state(repo_path=str(tmp_path), files=[]))
    titles = {f["title"] for f in out["findings"]}
    assert "No validators available" in titles   # deterministic kept
    assert "LLM: Untested edge case" in titles   # enrichment merged
    assert out["llm_calls"][0]["role"] == "qa"
    assert out["processes"] == []


def test_qa_specialist_enriches_normal_path(monkeypatch, tmp_path):
    """Validators found -> command loop runs -> enrichment still merges."""
    _fake_cfg(monkeypatch, meta={
        "text": '[{"severity": "info", "title": "Edge path untested",'
                ' "detail": "x", "file": null}]',
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        "finish_reason": "stop", "truncated": False, "duration_s": 0.2,
    })
    (tmp_path / "app.js").write_text("console.log(1)\n")
    monkeypatch.setattr(
        agents.mcp_tools, "spawn_process",
        lambda *a, **k: {"pgid": 0, "exit_code": 0, "timed_out": False,
                         "duration_s": 0.01, "stdout": "", "stderr": ""},
    )
    out = agents.qa_specialist(_state(
        repo_path=str(tmp_path),
        files=[{"filename": "app.js"}],
    ))
    titles = {f["title"] for f in out["findings"]}
    assert "Validation passed" in titles          # deterministic kept
    assert "LLM: Edge path untested" in titles    # enrichment merged
    assert out["llm_calls"][0]["role"] == "qa"
    assert len(out["processes"]) == 1             # one spawn recorded


# -- harness summarization ----------------------------------------------------- #

def test_harness_summarize_returns_text_and_record(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": "El PR introduce riesgo bajo; corregir el token falso.",
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
        "finish_reason": "stop", "truncated": False, "duration_s": 1.5,
    })
    findings = [{"agent": "security", "severity": "blocker",
                 "title": "Secret", "detail": "x"}]
    out = agents.harness_summarize(_state(), findings, approve=False)
    assert "riesgo" in out["text"]
    rec = out["llm_calls"][0]
    assert rec["role"] == "harness" and rec["node"] == "synthesizer"
    assert rec["status"] == "ok"
    assert rec["usage"]["prompt_tokens"] == 50


def test_harness_summarize_records_error(monkeypatch):
    _fake_cfg(monkeypatch, exc=RuntimeError("ollama down"))
    out = agents.harness_summarize(_state(), [], approve=True)
    assert out["text"] == ""
    assert out["llm_calls"][0]["status"] == "error"


def test_harness_summarize_disabled_is_silent(monkeypatch):
    monkeypatch.setattr(
        agents.llm.LLMConfig, "for_role",
        classmethod(lambda cls, role, env=None: agents.llm.LLMConfig()),
    )
    out = agents.harness_summarize(_state(), [], approve=True)
    assert out == {"text": "", "llm_calls": [], "active_pgids": []}


def test_synthesizer_records_harness_call(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": "Resumen del harness.",
        "usage": {"prompt_tokens": 9, "completion_tokens": 5},
        "finish_reason": "stop", "truncated": False, "duration_s": 0.5,
    })
    state = _state(findings=[
        {"agent": "review", "severity": "info", "title": "ok", "detail": "d"},
    ])
    out = synthesizer_node(state)
    assert out["verdict"]["llm_summary"] == "Resumen del harness."
    assert out["metrics"]["llm"]["calls"] == 1
    assert out["metrics"]["llm"]["detail"][0]["role"] == "harness"
    assert out["llm_calls"][0]["node"] == "synthesizer"


def test_strip_think_removes_reasoning_blocks():
    raw = "<think>\nlet me reason about this PR\nmore thought\n</think>\nFinal answer text."
    assert agents._strip_think(raw) == "Final answer text."


def test_strip_think_unclosed_block():
    raw = "<think>\nreasoning truncated mid-way\n\nActual summary here."
    assert agents._strip_think(raw) == "Actual summary here."


def test_harness_summarize_strips_think(monkeypatch):
    _fake_cfg(monkeypatch, meta={
        "text": "<think>analyzing…</think>Clean English summary.",
        "usage": {"prompt_tokens": 9, "completion_tokens": 5},
        "finish_reason": "stop", "truncated": False, "duration_s": 0.5,
    })
    out = agents.harness_summarize(_state(), [], approve=True)
    assert out["text"] == "Clean English summary."
    assert out["llm_calls"][0]["status"] == "ok"


def test_harness_digest_includes_finding_details():
    findings = [{
        "agent": "review", "severity": "blocker",
        "title": "LLM: Interpolación sin quoting",
        "detail": "sshUrl se interpola sin comillas en el comando.",
    }]
    msg = agents._harness_user_message(_state(), findings, approve=False)
    assert "LLM: Interpolación sin quoting" in msg
    assert "sin comillas" in msg          # detail content reaches prompt
    assert "blocker" in msg
