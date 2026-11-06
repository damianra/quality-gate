"""Deterministic vibecoding scanner tests (pure functions, no git/network)."""

from quality_gate_mcp.tools import analyze_diff


def _diff(files: dict[str, tuple[list[str], list[str]]]) -> str:
    """Build a minimal unified diff: {path: ([removed], [added])}."""
    out = []
    for path, (rem, add) in files.items():
        out.append(f"diff --git a/{path} b/{path}")
        out.append(f"--- a/{path}")
        out.append(f"+++ b/{path}")
        out.append("@@ -1,1 +1,1 @@")
        out.extend(f"-{l}" for l in rem)
        out.extend(f"+{l}" for l in add)
    return "\n".join(out)


def test_whitespace_only_diff_high_ratio():
    d = _diff({"src/app.ts": (["const x = 1;"], ["const   x   =   1;"])})
    s = analyze_diff(d)
    assert s["whitespace_ratio"] == 1.0
    assert not s["large_diff"]


def test_mixed_diff_lower_ratio():
    d = _diff(
        {
            "src/app.ts": (
                ["const x = 1;", "const y = 2;"],
                ["const   x = 1;", "const z = compute(y);"],
            )
        }
    )
    s = analyze_diff(d)
    assert 0.0 < s["whitespace_ratio"] < 1.0


def test_config_churn_flags_sensitive_files():
    d = _diff(
        {
            "tsconfig.json": (['"strict": false'], ['"strict": true']),
            "src/app.ts": (["a"], ["b"]),
            ".github/workflows/ci.yml": (["on: push"], ["on: pull_request"]),
        }
    )
    s = analyze_diff(d)
    assert "tsconfig.json" in s["config_churn"]
    assert ".github/workflows/ci.yml" in s["config_churn"]
    assert "src/app.ts" not in s["config_churn"]


def test_new_dependencies_detected_in_package_json():
    d = _diff(
        {
            "package.json": (
                [],
                ['"left-pad": "^1.3.0",', '"name": "my-app"'],
            )
        }
    )
    s = analyze_diff(d)
    assert "left-pad" in s["new_dependencies"]
    assert "my-app" not in s["new_dependencies"]  # no version constraint


def test_debug_leftovers_console_and_debugger():
    d = _diff(
        {
            "src/app.ts": (
                [],
                ["console.log('hi');", "debugger;", "const a = 1;"],
            )
        }
    )
    s = analyze_diff(d)
    assert len(s["debug_leftovers"]) == 2


def test_todos_added():
    d = _diff({"src/app.ts": ([], ["// TODO: refactor this", "const a = 1;"])})
    s = analyze_diff(d)
    assert any("TODO" in t for t in s["todos_added"])


def test_commented_blocks_run_detection():
    d = _diff(
        {
            "src/app.ts": (
                [],
                ["// const a = 1;", "// const b = 2;", "// const c = 3;", "const d = 4;"],
            )
        }
    )
    s = analyze_diff(d)
    assert len(s["commented_blocks"]) == 1
    assert s["commented_blocks"][0]["lines"] == 3


def test_secret_candidates():
    d = _diff(
        {
            "src/keys.ts": (
                [],
                ["const api_key = 'sk-12345';", "const url = 'https://x';"],
            )
        }
    )
    s = analyze_diff(d)
    assert len(s["secret_candidates"]) == 1


def test_large_diff_thresholds():
    many_files = {f"src/f{i}.ts": (["a"], ["b"]) for i in range(15)}
    s = analyze_diff(_diff(many_files))
    assert s["large_diff"]


def test_files_outside_hint_uses_pr_body():
    d = _diff(
        {
            "src/components/Button.tsx": (["a"], ["b"]),
            "scripts/deploy.sh": (["x"], ["y"]),
        }
    )
    body = "This PR touches src/components/Button.tsx only."
    s = analyze_diff(d, pr_body=body)
    assert "scripts/deploy.sh" in s["files_outside_hint"]
    assert "src/components/Button.tsx" not in s["files_outside_hint"]


def test_no_hints_no_outside_files():
    d = _diff({"anything/at/all.ts": (["a"], ["b"])})
    s = analyze_diff(d, pr_body="no file mentions here")
    assert s["files_outside_hint"] == []
