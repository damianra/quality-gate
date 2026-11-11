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

# Quoted specifiers (FR-L6): `from "s"`/`import "s"`/`import("s")` (JS,
# Go single-imports), `require("s")`/`require 's'`/`require_relative`
# (JS/Ruby/PHP), `include 's'`/`require_once` (PHP).
_QUOTED_SPEC_RE = re.compile(
    r"""(?:from\s+|import\s+|import\(\s*|require\w*\s*\(?\s*|"""
    r"""include\w*\s*\(?\s*)['"](?P<spec>[^'"]+)['"]"""
)
# Unquoted Python/Java specs: `import pkg.mod`, `from pkg.mod import x`.
_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<mod>[\w.]+)\s+import|import\s+(?P<mod2>[\w.]+))",
    re.MULTILINE,
)
# Go: quoted entries inside `import ( ... )` blocks — module paths carry
# at least one slash (stdlib names are never repo dependents anyway).
_GO_IMPORT_RE = re.compile(
    r'^\s*"([\w.-]+(?:/[\w./-]+)+)"', re.MULTILINE)
# Rust: `use crate::mod::item` / `mod name;` — `::` normalized to `/`.
_RUST_USE_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:use|mod)\s+([\w:]+)\s*;", re.MULTILINE)
# PHP: `use Vendor\Pkg` — backslashes normalized to `/`.
_PHP_USE_RE = re.compile(r"^\s*use\s+([\w\\]+)", re.MULTILINE)
# C#: `using Foo.Bar` (`using static` skipped).
_CS_USING_RE = re.compile(r"^\s*using\s+(?!static\b)([\w.]+)", re.MULTILINE)
_SCAN_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py",
    ".go", ".rs", ".java", ".rb", ".php", ".cs",
}
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
    if p.suffix == ".go":  # Go imports target the package dir, not files
        stems.add(p.parent.name)
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
        ext = path.suffix
        if ext == ".go":
            specs += [m.group(1) for m in _GO_IMPORT_RE.finditer(text)]
        elif ext == ".rs":
            specs += [
                m.group(1).replace("::", "/")
                for m in _RUST_USE_RE.finditer(text)
            ]
        elif ext == ".php":
            specs += [
                m.group(1).replace("\\", "/")
                for m in _PHP_USE_RE.finditer(text)
            ]
        elif ext == ".cs":
            specs += [m.group(1) for m in _CS_USING_RE.finditer(text)]
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
    of changed files while budget lasts, the repo's rules doc
    (``AGENTS.md``/``CLAUDE.md``, FR-C2) and direct dependents discovered
    in the repo — the first ``RELATED_HEAD_FILES`` of them with head
    excerpts (FR-C3). Hard limits: ``SCOPE_MAX_FILES`` files and
    ``SCOPE_MAX_CHARS`` total characters.
    """
    repo = Path(repo_path)
    changed_names = [f["filename"] for f in files]
    related = find_dependents(repo_path, changed_names)

    agents_doc = None
    for name in ("AGENTS.md", "CLAUDE.md"):
        doc_path = repo / name
        try:
            if doc_path.is_file():
                agents_doc = doc_path.read_text(
                    encoding="utf-8", errors="replace")[
                    : config.PROMPT_MAX_AGENTS_CHARS]
                break
        except OSError:
            continue

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

    # FR-C3: head excerpts of the first dependents so specialists can
    # inspect real callers — remaining names stay listed-only.
    related_heads: list[dict[str, str]] = []
    for rel in related[: config.RELATED_HEAD_FILES]:
        if budget <= 0:
            break
        try:
            head = (repo / rel).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        head = head[: min(config.RELATED_HEAD_CHARS, budget)]
        related_heads.append({"path": rel, "head": head})
        budget -= len(head)

    return {
        "diff_text": scoped_diff,
        "changed_files": changed_names,
        "file_contents": contents,
        "related_files": related,
        "related_heads": related_heads,
        "agents_doc": agents_doc,
        "truncated": truncated,
    }
