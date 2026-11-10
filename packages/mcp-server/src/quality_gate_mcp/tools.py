"""Deterministic MCP tools for PR review.

All tools are stateless: every call re-derives what it needs from the
repository at ``repo_path`` and the GitHub API. Nothing is persisted.

Pure helper functions (``analyze_diff``, ``build_verdict_comment``,
``find_verdict_comment``) are intentionally separated from I/O so they can
be unit-tested without git or network access.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from typing import Any

from . import config, git_ops
from .github_api import GitHubClient

# --------------------------------------------------------------------------- #
# Verdict comment contract
# --------------------------------------------------------------------------- #

MARKER_PREFIX = "<!-- pr-review head:"

# Per-check icons: blockers read as errors, warnings as warnings,
# info (or legacy checks without severity) as approval marks.
_CHECK_ICONS = {"blocker": "❌", "warning": "⚠️", "info": "✅"}
MARKER_RE = re.compile(r"<!--\s*pr-review head:(?P<sha>[0-9a-fA-F]{7,40})\s*-->")


def build_verdict_comment(
    approve: bool,
    checks: list[dict[str, Any]],
    result: str,
    note: str,
    head_sha: str,
    metrics_line: str = "",
    metrics_detail: str = "",
) -> str:
    """Build the contractual verdict comment body.

    Format (non-negotiable, English per FR-T1):
      ``## Functional validation`` heading, a verdict line, one bullet per
      check, Result/Note lines, the Metrics one-liner, an optional
      collapsible telemetry block (``metrics_detail``), and the
      ``<!-- pr-review head:<sha7> -->`` idempotency marker.
    """
    icon = "✅" if approve else "❌"
    verdict = "Approved" if approve else "Changes required"
    lines = [
        "## Functional validation",
        "",
        f"**Verdict:** {icon} {verdict}",
        "",
    ]
    for c in checks:
        mark = _CHECK_ICONS.get(c.get("severity")) or (
            "✅" if c.get("passed") else "❌"
        )
        detail = c.get("detail", "")
        lines.append(f"- {mark} **{c.get('name', 'check')}** — {detail}")
    if result:
        lines += ["", f"**Result:** {result}"]
    if note:
        lines.append(f"**Note:** {note}")
    if metrics_line:
        lines += ["", f"**Metrics:** {metrics_line}"]
    if metrics_detail:
        lines += [
            "",
            "<details><summary>LLM telemetry</summary>",
            "",
            metrics_detail,
            "",
            "</details>",
        ]
    lines += ["", f"{MARKER_PREFIX}{head_sha[:7]} -->"]
    return "\n".join(lines)


def find_verdict_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest comment containing a pr-review marker, if any."""
    marked = [c for c in comments if MARKER_RE.search(c.get("body", ""))]
    return marked[-1] if marked else None


def pr_review_status(comments: list[dict[str, Any]], head_sha: str) -> str:
    """``'pending'`` when no verdict comment matches ``head_sha``'s sha7."""
    marker = find_verdict_comment(comments)
    if marker is None:
        return "pending"
    m = MARKER_RE.search(marker["body"])
    return "reviewed" if m and head_sha[:7].lower() == m.group("sha").lower() else "pending"


# --------------------------------------------------------------------------- #
# Vibecoding signal scanner (pure functions)
# --------------------------------------------------------------------------- #

_SECRET_RE = re.compile(
    r"(api_key|apikey|token|secret|password|passwd|private_key)\s*[:=]\s*['\"]",
    re.IGNORECASE,
)
_DEBUG_RE = re.compile(r"console\.(log|debug)\s*\(|\bdebugger\b")
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_DEP_LINE_RE = re.compile(r'"([@./\w-]+)"\s*:\s*"[~^<>=\d]')
_COMMENT_PREFIXES = ("//", "#", "/*", "*", "*/", "<!--", "{/*")
_COMMENT_LEAD_RE = re.compile(r"^\s*(?://+|#+|/\*+|\*+|<!--|\{/\*)\s*")
_COMMENT_END_RE = re.compile(r"(?:\*/|-->)\s*$")
# A comment line only counts as commented-out code when its content
# looks like a statement (code tail, keyword + paren, or assignment);
# explanatory prose/doc comments must not flag "dead code".
_CODE_TAIL_RE = re.compile(r"[;{}()=<>]\s*$")
_CODE_KW_RE = re.compile(
    r"^(?:if|elif|else|for|while|switch|case|try|except|finally|do|with|"
    r"return|const|let|var|function|def|class|import|from|require|export|"
    r"await|async|new|throw|assert|print|static|public|private|void)\s*[(=({]"
)
_CODE_ASSIGN_RE = re.compile(r"^\s*[\w.\[\]'\"]+\s*=\s*\S")
# Docs are always in scope — AGENTS.md requires them updated in the
# same PR, so a path hinted only in the body must not flag them.
_DOC_PATH_RE = re.compile(
    r"(^|/)(readme|changelog|license|contributing|code_of_conduct|"
    r"security)(\.[^/]*)?$|\.md$|(^|/)docs?/",
    re.IGNORECASE,
)
_HINT_RE = re.compile(
    r"[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|json|jsonc|md|yml|yaml|css|scss|py|toml)"
)


def _is_sensitive(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(base, pat) for pat in config.SENSITIVE_CONFIGS):
        return True
    if any(fnmatch.fnmatch(path, pat) for pat in config.SENSITIVE_CONFIGS):
        return True
    return any(seg in path for seg in config.SENSITIVE_DIRS)


def _extract_hint_paths(pr_body: str) -> list[str]:
    """Pull file/dir mentions out of a PR body to approximate intended scope."""
    hints: set[str] = set()
    for m in _HINT_RE.finditer(pr_body or ""):
        hints.add(m.group(0).lstrip("./"))
    return sorted(hints)


def analyze_diff(diff_text: str, pr_body: str = "") -> dict[str, Any]:
    """Scan a unified diff and return deterministic vibecoding signals."""
    files: list[str] = []
    per_file: dict[str, dict[str, int]] = {}
    added: list[tuple[str, str]] = []   # (file, line) for '+' lines
    removed: list[tuple[str, str]] = []  # (file, line) for '-' lines

    current = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            # diff --git a/old b/new
            parts = raw.split(" b/")
            path = parts[-1] if len(parts) == 2 else raw.rsplit(" ", 1)[-1].lstrip("b/")
            current = path
            files.append(current)
            per_file[current] = {"added": 0, "removed": 0}
        elif raw.startswith(("+++", "---", "index ", "new file", "deleted file", "@@", "Binary")):
            continue
        elif raw.startswith("+") and current:
            added.append((current, raw[1:]))
            per_file[current]["added"] += 1
        elif raw.startswith("-") and current:
            removed.append((current, raw[1:]))
            per_file[current]["removed"] += 1

    added_lines = [l for _, l in added]
    removed_lines = [l for _, l in removed]
    total_changed = len(added_lines) + len(removed_lines)

    # whitespace_ratio: fraction of changed lines whose *stripped* content is
    # identical on both sides of the diff (whitespace-only edits).
    if total_changed:
        # Normalize by removing ALL whitespace: a line that only gained/lost
        # spaces or tabs is a whitespace-only edit.
        common = Counter("".join(l.split()) for l in added_lines) & Counter(
            "".join(l.split()) for l in removed_lines
        )
        whitespace_ratio = round(2 * sum(common.values()) / total_changed, 3)
    else:
        whitespace_ratio = 0.0

    # new_dependencies: added "name": "version" lines inside package.json
    new_deps = sorted(
        {
            m.group(1)
            for f, line in added
            if f.rsplit("/", 1)[-1] == "package.json"
            for m in [_DEP_LINE_RE.search(line)]
            if m
        }
    )

    debug_leftovers = [l for l in added_lines if _DEBUG_RE.search(l)]
    todos_added = [l.strip() for l in added_lines if _TODO_RE.search(l)]
    # (file, line) so downstream severity policy can scope by path —
    # e.g. token-looking fixtures inside test files are not blockers.
    secret_candidates = [
        {"file": f, "line": l.strip()}
        for f, l in added
        if _SECRET_RE.search(l)
    ]
    config_churn = [f for f in files if _is_sensitive(f)]

    # commented_blocks: runs of >= COMMENTED_BLOCK_MIN added comment-only
    # lines whose content looks like code (prose comments don't count)
    commented_blocks: list[dict[str, Any]] = []
    run_file, run_len, run_start = None, 0, None
    for f, line in added:
        stripped = line.strip()
        is_comment = bool(stripped) and stripped.startswith(_COMMENT_PREFIXES)
        if is_comment:
            inner = _COMMENT_LEAD_RE.sub("", stripped)
            inner = _COMMENT_END_RE.sub("", inner).strip()
            is_comment = bool(inner) and (
                _CODE_TAIL_RE.search(inner)
                or _CODE_KW_RE.match(inner)
                or _CODE_ASSIGN_RE.match(inner)
            )
        if is_comment and f == run_file:
            run_len += 1
        elif is_comment:
            run_file, run_len, run_start = f, 1, line.strip()[:60]
        else:
            if run_len >= config.COMMENTED_BLOCK_MIN:
                commented_blocks.append({"file": run_file, "lines": run_len, "sample": run_start})
            run_file, run_len, run_start = None, 0, None
    if run_len >= config.COMMENTED_BLOCK_MIN:
        commented_blocks.append({"file": run_file, "lines": run_len, "sample": run_start})

    # files_outside_hint: files not covered by any path hinted in the PR body
    hints = _extract_hint_paths(pr_body)
    if hints:
        files_outside_hint = [
            f
            for f in files
            if not _DOC_PATH_RE.search(f)
            and not any(h in f or f in h or f.rsplit("/", 1)[-1] == h.rsplit("/", 1)[-1] for h in hints)
        ]
    else:
        files_outside_hint = []

    large_diff = (
        len(files) > config.LARGE_DIFF_FILES
        or total_changed > config.LARGE_DIFF_LINES
    )

    return {
        "files": files,
        "per_file": per_file,
        "files_changed": len(files),
        "lines_changed": total_changed,
        "whitespace_ratio": whitespace_ratio,
        "large_diff": large_diff,
        "config_churn": config_churn,
        "new_dependencies": new_deps,
        "debug_leftovers": debug_leftovers,
        "todos_added": todos_added,
        "commented_blocks": commented_blocks,
        "secret_candidates": secret_candidates,
        "files_outside_hint": files_outside_hint,
        "scope_hints": hints,
    }


# --------------------------------------------------------------------------- #
# MCP tools (I/O wrappers)
# --------------------------------------------------------------------------- #


def list_prs(repo_path: str) -> list[dict[str, Any]]:
    """List open PRs for the repo, marking each 'pending' or 'reviewed'.

    A PR is ``pending`` when no verdict comment carries the
    ``<!-- pr-review head:<sha7> -->`` marker matching its current head.
    """
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        out = []
        for pr in gh.list_pulls(slug):
            comments = gh.list_issue_comments(slug, pr["number"])
            head = pr["head"]["sha"]
            out.append(
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "head_sha": head,
                    "status": pr_review_status(comments, head),
                    "url": pr["html_url"],
                }
            )
        return out


def get_pr_diff(repo_path: str, pr_number: int, page: int = 1, per_page: int = 30) -> dict[str, Any]:
    """Return PR stats and the paginated file list (no inline patches)."""
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        pr = gh.get_pr(slug, pr_number)
        files = gh.list_pr_files(slug, pr_number, page=page, per_page=per_page)
        return {
            "pr_number": pr_number,
            "title": pr["title"],
            "head_sha": pr["head"]["sha"],
            "base_ref": pr["base"]["ref"],
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "changed_files": pr["changed_files"],
            "page": page,
            "files": [
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                    "changes": f["changes"],
                }
                for f in files
            ],
        }


def get_file_diff(repo_path: str, pr_number: int, file_path: str, max_pages: int = 10) -> dict[str, Any]:
    """Return the unified patch for a single file of the PR."""
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        for page in range(1, max_pages + 1):
            files = gh.list_pr_files(slug, pr_number, page=page, per_page=100)
            for f in files:
                if f["filename"] == file_path:
                    return {
                        "filename": file_path,
                        "status": f["status"],
                        "additions": f["additions"],
                        "deletions": f["deletions"],
                        "patch": f.get("patch"),  # None when GitHub truncates
                    }
            if len(files) < 100:
                break
    return {"filename": file_path, "status": "not_found", "patch": None}


def analyze_pr_signals(repo_path: str, pr_number: int) -> dict[str, Any]:
    """Run the deterministic vibecoding scanner over a PR's diff."""
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        pr = gh.get_pr(slug, pr_number)
        diff_text = gh.get_pr_diff_text(slug, pr_number)
        signals = analyze_diff(diff_text, pr.get("body") or "")
        signals.update(
            {
                "pr_number": pr_number,
                "title": pr["title"],
                "head_sha": pr["head"]["sha"],
                "base_ref": pr["base"]["ref"],
                "pr_body": pr.get("body") or "",
                # Raw unified diff: primary evidence for the scoped
                # review context built by the orchestrator router.
                "diff_text": diff_text,
            }
        )
        return signals


def search_code(repo_path: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Fast ripgrep search over the checked-out code base."""
    res = git_ops.run_subprocess(
        repo_path,
        ["rg", "--line-number", "--no-heading", "--color", "never", "--", query],
        timeout=config.RG_TIMEOUT,
    )
    if res["timed_out"] or res["exit_code"] not in (0, 1):
        return []
    matches: list[dict[str, Any]] = []
    for line in res["stdout"].splitlines():
        file, _, rest = line.partition(":")
        lineno, _, text = rest.partition(":")
        if not lineno.isdigit():
            continue
        matches.append({"file": file, "line": int(lineno), "text": text.strip()})
        if len(matches) >= limit:
            break
    return matches


def spawn_process(
    repo_path: str, command: str, args: list[str] | None = None, timeout: int | None = None
) -> dict[str, Any]:
    """Spawn ``command`` in a NEW process group inside ``repo_path``.

    Returns ``pgid`` plus stdout/stderr/exit code. The caller MUST register
    the pgid in ``active_pgids`` so the orchestrator's cleanup node can
    terminate the group. ``shell=True`` is never used.
    """
    return git_ops.spawn_tracked(repo_path, command, args or [], timeout=timeout)


def post_verdict(
    repo_path: str,
    pr_number: int,
    approve: bool,
    checks: list[dict[str, Any]],
    result: str = "",
    note: str = "",
    metrics_line: str = "",
    metrics_detail: str = "",
) -> dict[str, Any]:
    """Post (or update) the contractual verdict comment on the PR.

    Idempotent: the latest comment containing a pr-review marker is
    updated in place WHILE it remains the most recent comment in the
    thread; if the thread continued after it (someone replied), a fresh
    comment is posted instead of rewriting history above the reply.
    """
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        pr = gh.get_pr(slug, pr_number)
        head_sha = pr["head"]["sha"]
        body = build_verdict_comment(
            approve, checks, result, note, head_sha,
            metrics_line=metrics_line, metrics_detail=metrics_detail,
        )
        comments = gh.list_issue_comments(slug, pr_number)
        existing = find_verdict_comment(comments)
        if existing is not None and comments[-1]["id"] != existing["id"]:
            # The thread continued after our verdict — keep the old
            # comment untouched and post a fresh one below the reply.
            existing = None
        if existing is not None:
            comment = gh.update_issue_comment(slug, existing["id"], body)
            action = "updated"
        else:
            comment = gh.create_issue_comment(slug, pr_number, body)
            action = "created"
        return {"action": action, "url": comment["html_url"], "head_sha": head_sha[:7]}


def create_pr(
    repo_path: str, head: str, title: str, body: str = "", base: str = "main"
) -> dict[str, Any]:
    """Create a pull request (returns the existing one on 422 conflicts)."""
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        pr = gh.create_pull(slug, head=head, base=base, title=title, body=body)
        return {"number": pr["number"], "url": pr["html_url"], "head": head, "base": base}
