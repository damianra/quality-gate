---
description: validate a pull request like an autonomous reviewer sub-agent
---

# PR review sub-agent

Reviews the current branch (or an open PR) the way `pr-review.yml` + the
LangGraph orchestrator do in CI, but locally and interactively. The final
verdict is reported to the user **in Spanish**; everything written to files
or commits stays **in English** per `AGENTS.md`.

## Steps

1. Determine the PR scope: the base branch is `main` unless told otherwise.

// turbo
2. Inspect what changed:
   `git status --short && git log --oneline origin/main..HEAD && git diff origin/main...HEAD --stat`

// turbo
3. Run the test suite before judging anything:
   `uv run pytest packages -q`

// turbo
4. Run the deterministic vibecoding scanner on the diff — the same
   `analyze_diff` the MCP `analyze_pr_signals` tool uses, here fed by local
   git (no GitHub API needed). Pass the PR body via `PR_BODY` when known so
   `files_outside_hint` can check declared scope:
   `PR_BODY="$(git log -1 --format=%B)" uv run python -c 'import os,subprocess; from quality_gate_mcp.tools import analyze_diff; import json; print(json.dumps(analyze_diff(subprocess.run(["git","diff","origin/main...HEAD"],capture_output=True,text=True).stdout, os.environ.get("PR_BODY","")), indent=2))'`

// turbo
5. Backstop grep for patterns the scanner does not cover:
   `git diff origin/main...HEAD | grep -nE 'shell=True|GITHUB_TOKEN\s*=|DATABASE_URL\s*='`

6. Act as three specialists and produce findings. Interpret the step-4
   signal dict — every non-empty signal is a finding:

   - **ReviewAgent** — per `AGENTS.md`: comments and docs in English;
     `README.md` and `README_ES.md` updated together when either changes.
     Vibecoding findings:
     - `whitespace_ratio` >= 0.6 -> warning (reformatting noise)
     - `large_diff` -> warning (PR should be split)
     - `commented_blocks` -> warning (dead commented code)
     - `debug_leftovers` -> warning (`console.log`/`debugger`)
     - `todos_added` -> info (must reference an issue or be scoped)
     - `files_outside_hint` -> warning (outside declared PR scope)
   - **SecurityAgent** — no `shell=True`; every spawned process must run in
     its own process group and register its pgid; no secrets or `QG_*`
     values outside env vars / `.env.example`. Signal findings:
     - `secret_candidates` -> **blocker** (hardcoded credential)
     - `new_dependencies` -> warning (verify provenance/license)
     - `config_churn` -> warning (sensitive config touched)
   - **QAAgent** — tests added or updated for behavior changes; the suite
     from step 3 must be green; new code paths that spawn processes must
     have a cleanup assertion.

7. Emit the verdict **in Spanish** using the contractual format:

   ```markdown
   ## Validación funcional

   **Veredicto:** ✅ Aprobado | ❌ Cambios requeridos

   - ✅/❌ **check** — detalle

   **Result:** <resumen>
   **Note:** <nota>
   ```

   `Aprobado` requires: green tests, no blocker finding, and README
   alignment when docs changed. List every blocker with file and line.
