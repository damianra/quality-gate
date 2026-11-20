"""FR-O33 ruleset contract — enforced without a scanner.

Every ``qg-rules/*.yaml`` must parse, every rule must carry the
required keys, unique ``<lang>-<slug>`` ids, a known severity and
``metadata.source`` matching a family declared in
``qg-rules/PROVENANCE.md``. ``Dockerfile.sandbox`` additionally runs
``semgrep --validate`` at image build — this test is the CI-level
mirror for malformed shapes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

RULES_DIR = (
    Path(__file__).resolve().parent.parent
    / "src/pr_review_orch/qg-rules"
)

_SEVERITIES = {"INFO", "WARNING", "ERROR"}
_LANGUAGES = {"python", "javascript", "typescript", "go", "generic"}
_ID_RE = re.compile(r"^(python|js|go|generic)-[a-z0-9]+(-[a-z0-9]+)*$")
_PATTERN_KEYS = {"pattern", "patterns", "pattern-either",
                "pattern-regex", "pattern-inside",
                "pattern-sources", "pattern-sinks"}


def _rules() -> list[dict]:
    out = []
    for f in sorted(RULES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text())
        assert isinstance(doc, dict) and isinstance(
            doc.get("rules"), list), f"{f.name}: no rules: list"
        for r in doc["rules"]:
            out.append((f.name, r))
    return out


def _provenance_families() -> set[str]:
    """First-column ids of the PROVENANCE.md family table."""
    text = (RULES_DIR / "PROVENANCE.md").read_text()
    fams = set()
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 3 and cells[0] == "" and cells[1] not in (
                "", "family") and not set(cells[1]) <= {"-"}:
            fams.add(cells[1])
    return fams


def test_every_rule_has_required_keys():
    for fname, r in _rules():
        for key in ("id", "message", "languages", "severity",
                    "metadata"):
            assert key in r, f"{fname}:{r.get('id')} missing {key}"
        assert _PATTERN_KEYS & r.keys(), \
            f"{fname}:{r['id']} has no pattern key"


def test_rule_ids_unique_and_conventional():
    ids = [r["id"] for _, r in _rules()]
    assert len(ids) == len(set(ids)), "duplicate rule ids"
    for fname, r in _rules():
        assert _ID_RE.match(r["id"]), \
            f"{fname}:{r['id']} breaks the <lang>-<slug> convention"


def test_severity_and_languages_known():
    for fname, r in _rules():
        assert r["severity"] in _SEVERITIES, \
            f"{fname}:{r['id']} severity {r['severity']!r}"
        langs = r["languages"]
        assert isinstance(langs, list) and langs, \
            f"{fname}:{r['id']} empty languages"
        assert set(langs) <= _LANGUAGES, \
            f"{fname}:{r['id']} languages {langs!r}"


def test_metadata_source_in_provenance_table():
    fams = _provenance_families()
    assert fams, "PROVENANCE.md family table is empty"
    for fname, r in _rules():
        src = r["metadata"].get("source") \
            if isinstance(r["metadata"], dict) else None
        assert src in fams, \
            f"{fname}:{r['id']} metadata.source {src!r} not in " \
            f"PROVENANCE.md {sorted(fams)}"
        cwe = r["metadata"].get("cwe")
        assert type(cwe) is int and cwe > 0, \
            f"{fname}:{r['id']} metadata.cwe {cwe!r}"


def test_provenance_table_covers_used_sources_only():
    """Every family id in the table is actually used — the license
    accounting must not list dead entries."""
    used = {r["metadata"]["source"] for _, r in _rules()}
    declared = _provenance_families()
    unused = declared - used
    assert not unused, f"PROVENANCE.md lists unused families: {unused}"
