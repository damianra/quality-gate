"""Deterministic MCP tools for PR review.

All tools are stateless: every call re-derives what it needs from the
repository at ``repo_path`` and the GitHub API. Nothing is persisted.

Pure helper functions (``analyze_diff``, ``build_verdict_comment``,
``find_verdict_comment``) are intentionally separated from I/O so they can
be unit-tested without git or network access.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path, PurePosixPath
from collections import Counter
from typing import Any

from . import config, git_ops
from .github_api import GitHubClient

log = logging.getLogger(__name__)

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
# FR-L3 debug leftovers across languages — unambiguous debugger
# statements only; ordinary output calls (print, fmt.Println) are too
# false-positive prone to flag.
_DEBUG_RE = re.compile(
    r"console\.(?:log|debug)\s*\(|\bdebugger\b"     # JS/TS
    r"|\bbreakpoint\s*\(|pdb\.set_trace\b"           # Python
    r"|\bbinding\.pry\b|\bbyebug\b"                  # Ruby
    r"|\bdbg!\s*\("                                  # Rust
)
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
# FR-L4 dependency-line detectors, keyed by manifest basename.
_DEP_LINE_RE = re.compile(r'"([@./\w-]+)"\s*:\s*"[~^<>=\d]')
_REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][\w.-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~]=?|;|@|$)")
_PEP508_QUOTED_RE = re.compile(
    r"^\s*[\"']([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?\s*(?:[<>=!~]=?|@|;)")
_GO_REQUIRE_RE = re.compile(
    r"^\s*(?:require\s+)?\(?\s*([\w.-]+(?:/[\w.-]+)+)\s+v[\w.-]+")
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
# FR-L5 scope hints: any path-like token counts, not a fixed extension
# list — a file hint needs a dotted suffix containing a letter, a
# directory hint needs a slash. Letter lookaheads keep version strings
# ("1.2.3", "v1.2.3") out.
_HINT_RES = [
    # File hint: path token whose dotted suffix contains a letter —
    # "1.2.3" and "v1.2.3" have digit-only suffixes and don't count.
    re.compile(r"(?=[\w./-]*[A-Za-z_])[\w./-]+\.(?=[A-Za-z0-9]*[A-Za-z_])"
               r"[A-Za-z0-9]{1,6}"),
    re.compile(r"(?=[\w./-]*[A-Za-z_])[\w.-]+(?:/[\w.-]+)+"),
]

# FR-C6..C8 inspection signals: reads the same QG_QA_CODE_EXTS env
# override as the orchestrator QA gate so signals and QA activation
# agree on what "code" means.
_CODE_EXTS = frozenset(
    "." + e.strip().lstrip(".").lower()
    for e in os.environ.get(
        "QG_QA_CODE_EXTS",
        ".ts,.tsx,.js,.jsx,.mjs,.cjs,.py,.go,.rs,.java,.kt,.rb,.php,"
        ".cs,.swift,.c,.cc,.cpp,.h,.hpp",
    ).replace(",", " ").split()
    if e.strip()
)
# Test-file path conventions (FR-C6). Case-sensitive on purpose: the
# Java/Kotlin/C# convention capitalizes 'Test'/'Tests' (FooTest.java),
# and matching case-insensitively would flag latest.py, contest.rs or
# greatest.js as test files.
_TEST_FILE_RE = re.compile(
    # dir names are case-insensitive (Tests/ is a common .NET/Java
    # convention); the Test/Tests *suffix* stays case-sensitive.
    r"(?i:(^|/)(tests?|__tests__|spec)/)|"
    r"(^|[._-])(test|spec)\.[a-z]+$|"
    r"(^|/)test_|_test\.[a-z]+$|_spec\.[a-z]+$|"
    r"(^|/)Test\w*\.|Tests?\.[a-z]+$",
)
# FR-C7: new error-handling branches per language family.
_ERR_BRANCH_RES = [
    # JS/TS + Java-family: catch (incl. ES2019 'catch {'), throw,
    # promise reject, Result.Err / bare Err(
    re.compile(r"if\s*\(\s*!\s*\w+(?:\.\w+)*\.ok\b|"
               r"\bcatch\s*[({]|\bthrow\s|\.reject\s*\(|\bErr\s*\("),
    # Python: except/raise at statement start; sys/os exit anywhere;
    # '|| raise' covers the Ruby modifier idiom.
    re.compile(r"^\s*(?:except\b|raise\b)|\|\|\s*raise\b|"
               r"\bsys\.exit\s*\(|\bos\._exit\b"),
    # Go: err checks and panic(
    re.compile(r"if\s+err\s*!=\s*nil|\breturn\s+err\b|\bpanic\s*\("),
    # Rust panic! + Ruby rescue
    re.compile(r"\bpanic!|\brescue\b"),
]
# FR-C8: destructive->recreative verb pairs among added lines. Order
# matters: the destructive call must be added *before* the recreative
# one (adding create-then-drop is not the flagged pattern).
_MUTATION_PAIRS = [
    ("remove->add", re.compile(r"\bremove\b", re.IGNORECASE),
     re.compile(r"\badd\b", re.IGNORECASE)),
    ("drop->create", re.compile(r"\bdrop\b", re.IGNORECASE),
     re.compile(r"\bcreate\b", re.IGNORECASE)),
    ("delete->insert", re.compile(r"\bdelete\b", re.IGNORECASE),
     re.compile(r"\binsert\b", re.IGNORECASE)),
    ("unlink->write", re.compile(r"\bunlink\s*\("),
     re.compile(r"\b(?:write|create)\w*\s*\(", re.IGNORECASE)),
    ("rm->mkdir", re.compile(r"\brm\s+"),
     re.compile(r"\bmkdir\b|\bwrite\w*\s*\(")),
]
# Caps keep the signals JSON bounded inside specialist prompts (same
# rationale as prompt_injection[:20]).
_MAX_RELATED_TESTS = 50
_MAX_UNTESTED_PATHS = 50
_MAX_STATE_MUTATIONS = 25


def _is_test_path(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(path))


def _test_candidates(src_path: str) -> list[str]:
    """Candidate test paths for a changed code file (FR-C6)."""
    p = PurePosixPath(src_path)
    stem, ext = p.stem, p.suffix
    # PurePosixPath('foo.py').parent is '.' — root files use no prefix.
    d = "" if str(p.parent) == "." else str(p.parent) + "/"
    return {
        ".py": [f"{d}test_{stem}.py", f"{d}{stem}_test.py",
                f"tests/test_{stem}.py"],
        ".go": [f"{d}{stem}_test.go"],
        ".rs": [f"{d}{stem}_test.rs", f"tests/{stem}.rs"],
        ".rb": [f"{d}{stem}_spec.rb", f"spec/{stem}_spec.rb"],
    }.get(ext, []) + [
        f"{d}{stem}.test{ext}", f"{d}{stem}.spec{ext}",
        f"{d}__tests__/{stem}{ext}",
        f"{d}{stem}Test{ext}", f"{d}Test{stem}{ext}",
        f"{d}{stem}Tests{ext}", f"tests/{stem}Test{ext}",
    ]

# Prompt-injection patterns (FR-O25): added lines that resemble
# directives aimed at the reviewer LLMs rather than at a compiler or a
# human. Matches are warnings, not blockers — legit docs may quote the
# same phrases — but the attempt is always reported.
_INJECTION_RES = [
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b"
        r"[^.\n]{0,60}\b(?:previous|prior|above|earlier|preceding|all|every|these|those)\b"
        r"[^.\n]{0,60}\b(?:instructions?|rules?|guidelines?|prompts?|guardrails?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\s+"
        r"(?:all|any|the|your|these|those|previous|prior)\s+"
        r"(?:instructions?|rules?|guidelines?|prompts?|guardrails?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:you are now|from now on you|your new (?:task|instructions|goal|objective))\b", re.IGNORECASE),
    re.compile(r"\bnew\s+(?:system\s+)?instructions?\s*[:.]", re.IGNORECASE),
    # Chat-template role markers — no legitimate diff should ship these.
    re.compile(r"<\|(?:im_start|im_end|endoftext)\|>|\[/?INST\]|<</?SYS>>"),
    # Verdict-forging: a standalone `VERDICT: approve` line mimics the
    # reviewer output contract; `VERDICT: approve|reject` doc samples
    # with trailing text do not match (EOL anchor).
    re.compile(r"\bverdict\s*:\s*(?:approved?|changes\s+required)\s*$", re.IGNORECASE),
]


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
    for rx in _HINT_RES:
        for m in rx.finditer(pr_body or ""):
            hints.add(m.group(0).lstrip("./"))
    return sorted(hints)


def _dep_name(basename: str, line: str) -> str | None:
    """Dependency name an added line declares in a manifest (FR-L4)."""
    if basename == "package.json":
        m = _DEP_LINE_RE.search(line)
        return m.group(1) if m else None
    if basename.startswith("requirements") and basename.endswith(".txt"):
        if line.lstrip().startswith(("#", "-")):
            return None  # comment or -r/-e/--option line
        m = _REQ_LINE_RE.match(line)
        return m.group(1) if m else None
    if basename == "pyproject.toml":
        # Quoted PEP-508 entries carrying a version specifier or a
        # direct/environment marker — bare quoted strings could be
        # keyword/classifier arrays, so they are not enough.
        m = _PEP508_QUOTED_RE.match(line)
        return m.group(1) if m else None
    if basename == "go.mod":
        # `require mod v1.2.3` and bare block entries `mod v1.2.3`.
        m = _GO_REQUIRE_RE.match(line)
        return m.group(1) if m else None
    return None


def analyze_diff(diff_text: str, pr_body: str = "",
                 repo_path: str = "") -> dict[str, Any]:
    """Scan a unified diff and return deterministic vibecoding signals."""
    files: list[str] = []
    per_file: dict[str, dict[str, int]] = {}
    added: list[tuple[str, str]] = []   # (file, line) for '+' lines
    removed: list[tuple[str, str]] = []  # (file, line) for '-' lines

    current = None
    deleted: set[str] = set()
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            # diff --git a/old b/new
            parts = raw.split(" b/")
            path = parts[-1] if len(parts) == 2 else raw.rsplit(" ", 1)[-1].lstrip("b/")
            current = path
            files.append(current)
            per_file[current] = {"added": 0, "removed": 0}
        elif raw.startswith("+++"):
            # `+++ /dev/null` marks a file deleted by this PR — it must
            # not count as existing coverage in related_tests.
            if raw.strip() == "+++ /dev/null" and current:
                deleted.add(current)
        elif raw.startswith(("---", "index ", "new file", "deleted file", "@@", "Binary")):
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

    # FR-L4 new_dependencies: added dependency entries in any supported
    # manifest -> {name, manifest} dicts.
    dep_pairs = {
        (name, f)
        for f, line in added
        for name in [_dep_name(f.rsplit("/", 1)[-1], line) or ""]
        if name
    }
    new_deps = [
        {"name": n, "manifest": f} for n, f in sorted(dep_pairs)
    ]

    debug_leftovers = [l for l in added_lines if _DEBUG_RE.search(l)]
    todos_added = [l.strip() for l in added_lines if _TODO_RE.search(l)]
    # (file, line) so downstream severity policy can scope by path —
    # e.g. token-looking fixtures inside test files are not blockers.
    secret_candidates = [
        {"file": f, "line": l.strip()}
        for f, l in added
        if _SECRET_RE.search(l)
    ]
    # FR-O25: prompt-injection attempts — added lines carrying LLM
    # directives or chat-template markers. Deterministic so the attempt
    # is reported even if a reviewer model obeys it.
    prompt_injection = [
        {"file": f, "line": l.strip()}
        for f, l in added
        if any(rx.search(l) for rx in _INJECTION_RES)
    ][:20]
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

    # FR-C6: source->test mapping by naming convention. Test existence
    # is checked in the diff and (when repo_path is given) on disk —
    # the disk lookup reflects the local checkout state, which in
    # service mode is the PR head worktree but in local mode is
    # whatever branch is checked out.
    changed = set(files) - deleted
    code_files = [
        f for f in files
        if PurePosixPath(f).suffix in _CODE_EXTS
        and not _is_test_path(f) and f not in deleted
    ]
    related_tests: list[dict[str, Any]] = []
    for src in code_files[:_MAX_RELATED_TESTS]:
        cands = _test_candidates(src)
        tests = [
            t for t in cands
            if t in changed
            or (repo_path and (Path(repo_path) / t).is_file())
        ]
        related_tests.append({
            "source": src,
            "tests": sorted(set(tests)),
            "test_changed": any(t in changed for t in cands),
        })

    # FR-C7: error-handling branches added in code files; "covered" is a
    # naming heuristic — a related test exists AND changed in this PR.
    covered_map = {
        rt["source"]: bool(rt["tests"]) and rt["test_changed"]
        for rt in related_tests
    }
    untested_paths: list[dict[str, Any]] = []
    for f, line in added:
        if (PurePosixPath(f).suffix not in _CODE_EXTS
                or _is_test_path(f) or f in deleted):
            continue
        for rx in _ERR_BRANCH_RES:
            if rx.search(line):
                untested_paths.append({
                    "file": f, "line": line.strip()[:120],
                    "kind": "error-branch",
                    "covered": covered_map.get(f, False),
                })
                break
        if len(untested_paths) >= _MAX_UNTESTED_PATHS:
            break

    # FR-C8: destructive->recreative pairs among added lines of a file;
    # the destructive line must be added *before* the recreative one.
    added_by_file: dict[str, list[str]] = {}
    for f, line in added:
        added_by_file.setdefault(f, []).append(line)
    state_mutations: list[dict[str, Any]] = []
    for f, flines in added_by_file.items():
        if (PurePosixPath(f).suffix not in _CODE_EXTS
                or _is_test_path(f)):
            continue
        for name, dst_rx, rec_rx in _MUTATION_PAIRS:
            dst_i = next(
                (i for i, l in enumerate(flines) if dst_rx.search(l)),
                None)
            # search the dst line itself too — 'rm -rf d && mkdir d'
            # is the same pattern on one added line.
            rec = next(
                (l for l in flines[dst_i:] if rec_rx.search(l)),
                None) if dst_i is not None else None
            if dst_i is not None and rec:
                state_mutations.append({
                    "file": f,
                    "remove_line": flines[dst_i].strip()[:120],
                    "add_line": rec.strip()[:120],
                    "pair": name,
                })
        if len(state_mutations) >= _MAX_STATE_MUTATIONS:
            break


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
        "prompt_injection": prompt_injection,
        "files_outside_hint": files_outside_hint,
        "scope_hints": hints,
        "related_tests": related_tests,
        "untested_paths": untested_paths,
        "state_mutations": state_mutations,
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
        signals = analyze_diff(
            diff_text, pr.get("body") or "", repo_path=repo_path)
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


def post_commit_status(
    slug: str, sha: str, state: str, description: str = "",
    context: str = "quality-gate",
) -> dict[str, Any]:
    """Post a commit status on ``sha`` (FR-P8: real merge gating).

    ``state`` is one of ``pending``/``success``/``failure``/``error``.
    Best-effort — callers swallow exceptions and log; the verdict
    comment remains the human-readable channel (INV-4).
    """
    with GitHubClient() as gh:
        return gh.create_commit_status(
            slug, sha, state, context=context, description=description)


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
        review = _submit_pr_review(gh, slug, pr_number, approve, body)
        return {
            "action": action,
            "url": comment["html_url"],
            "head_sha": head_sha[:7],
            "review": review,
        }


def _submit_pr_review(
    gh: GitHubClient, slug: str, pr_number: int, approve: bool, body: str
) -> str:
    """FR-O26: optional GitHub PR review submission — never fatal.

    Returns the outcome: ``off`` (disabled), the submitted event
    lowercased (``approve``/``comment``), or ``failed`` (e.g. self-approve
    422 — GitHub never lets a token approve its owner's own PR). The
    verdict comment is the durable artifact; a failed review only logs.
    """
    action = config.PR_REVIEW_ACTION
    if action == "off":
        return "off"
    event = "APPROVE" if action == "approve" and approve else "COMMENT"
    try:
        gh.submit_pr_review(slug, pr_number, event=event, body=body[:4000])
        return event.lower()
    except Exception as exc:
        log.warning("PR review submission failed (%s): %s", event, exc)
        return "failed"


def create_pr(
    repo_path: str, head: str, title: str, body: str = "", base: str = "main"
) -> dict[str, Any]:
    """Create a pull request (returns the existing one on 422 conflicts)."""
    slug = git_ops.get_remote_slug(repo_path)
    with GitHubClient() as gh:
        pr = gh.create_pull(slug, head=head, base=base, title=title, body=body)
        return {"number": pr["number"], "url": pr["html_url"], "head": head, "base": base}
