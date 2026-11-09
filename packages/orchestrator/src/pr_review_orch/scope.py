"""Blast-radius scoping: reviewers and sub-agents see only what the PR
touches plus its direct dependents — never the whole repository.

Keeping context bounded is what makes LLM-backed review affordable: a
whole-repo prompt would burn tokens and wall-clock time for no verdict
gain. The caps (``QG_SCOPE_MAX_FILES`` / ``QG_SCOPE_MAX_CHARS``) are hard;
``truncated`` tells callers when they kicked in.

Dependent discovery is pure Python (no ripgrep required): it scans
import/require specifiers and matches them against the changed files'
module stems.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config

# Quoted specifiers: `from "s"` (ES import/export), `import "s"`,
# `require("s")`, dynamic `import("s")`.
_QUOTED_SPEC_RE = re.compile(
    r"""(?:from\s+|import\s+|require\(\s*|import\(\s*)"""
    r"""['"](?P<spec>[^'"]+)['"]"""
)
# Unquoted Python specs: `import pkg.mod`, `from pkg.mod import x`.
_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<mod>[\w.]+)\s+import|import\s+(?P<mod2>[\w.]+))",
    re.MULTILINE,
)
_SCAN_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"}
_SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "__pycache__",
    ".venv", "coverage", ".next", ".expo",
}
_MAX_SCAN_FILES = 5000


def _module_stems(path: str) -> set[str]:
    """Specifiers that could resolve to ``path`` (relative or package)."""
    p = Path(path)
    stems = {p.stem, str(p.with_suffix(""))}
    if p.stem == "index":  # `import "pkg/dir"` resolves to dir/index.*
        stems.add(p.parent.name)
        stems.add(str(p.parent))
    return {s.lstrip("./") for s in stems if s}


def _imports_target(spec: str, targets: set[str]) -> bool:
    spec = spec.lstrip("./")
    return any(
        spec == t or spec.endswith("/" + t) or spec.endswith("." + t)
        or spec.startswith(t + "/")
        for t in targets
    )


def find_dependents(repo_path: str, changed_files: list[str]) -> list[str]:
    """Repo files outside the diff that directly import a changed file."""
    repo = Path(repo_path)
    targets: set[str] = set()
    for f in changed_files:
        targets |= _module_stems(f)
    if not targets:
        return []

    changed = set(changed_files)
    dependents: list[str] = []
    scanned = 0
    for path in sorted(repo.rglob("*")):
        if scanned >= _MAX_SCAN_FILES or len(dependents) >= config.SCOPE_MAX_FILES:
            break
        if not path.is_file() or path.suffix not in _SCAN_EXTS:
            continue
        if any(seg in _SKIP_DIRS for seg in path.parts):
            continue
        rel = path.relative_to(repo).as_posix()
        if rel in changed:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        specs = [m.group("spec") for m in _QUOTED_SPEC_RE.finditer(text)]
        specs += [
            spec
            for m in _PY_IMPORT_RE.finditer(text)
            for spec in m.groups()
            if spec
        ]
        if any(_imports_target(spec, targets) for spec in specs):
            dependents.append(rel)
    return dependents


def build_pr_context(
    repo_path: str,
    files: list[dict[str, Any]],
    diff_text: str,
) -> dict[str, Any]:
    """Bounded PR context for reviewers/sub-agents.

    Contents: the (truncated) diff, the list of changed files, full content
    of changed files while budget lasts, and direct dependents discovered
    in the repo. Hard limits: ``SCOPE_MAX_FILES`` files and
    ``SCOPE_MAX_CHARS`` total characters.
    """
    repo = Path(repo_path)
    changed_names = [f["filename"] for f in files]
    related = find_dependents(repo_path, changed_names)

    budget = config.SCOPE_MAX_CHARS
    # The diff is primary evidence: it gets up to half the total budget.
    diff_budget = budget // 2
    truncated = len(diff_text) > diff_budget
    scoped_diff = diff_text[:diff_budget]
    budget -= len(scoped_diff)

    contents: dict[str, str] = {}
    for f in files:
        if len(contents) >= config.SCOPE_MAX_FILES or budget <= 0:
            truncated = True
            break
        rel = f["filename"]
        if f.get("status") == "removed":
            continue  # deleted files have no current content to attach
        path = repo / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > budget:
            text = text[:budget]
            truncated = True
        contents[rel] = text
        budget -= len(text)

    return {
        "diff_text": scoped_diff,
        "changed_files": changed_names,
        "file_contents": contents,
        "related_files": related,
        "truncated": truncated,
    }
