---
description: validate a pull request like an autonomous reviewer sub-agent
---

# PR review sub-agent

Reviews the current branch (or an open PR) the way `pr-review.yml` + the
LangGraph orchestrator do in CI, but locally and interactively. The final
verdict is reported to the user **in Spanish**; everything written to files
or commits stays **in English** per `AGENTS.md`.

The actual review **must be performed by a delegated sub-agent** with a
fresh context: the parent agent gathers the raw evidence (diff, test
results, scanner output), hands it to the sub-agent, and only formats its
findings into the final verdict. Do not review inline in the parent agent.

## Steps

1. Determine the PR scope: the base branch is `main` unless told otherwise.

// turbo
2. Inspect what changed:
   `git status --short && git log --oneline origin/main..HEAD && git diff origin/main...HEAD --stat`

// turbo
3. Run the test suite before judging anything:
   `uv run pytest packages -q`

// turbo
4. Search for forbidden patterns across the diff:
   `git diff origin/main...HEAD | grep -nE 'shell=True|GITHUB_TOKEN|DATABASE_URL|console\.log|debugger|TODO|FIXME|HACK|XXX'`

5. **Delegate to the review sub-agent.** Spawn a sub-agent with this input:
   the step-2 diff, the step-3 test output, the step-4 grep hits, the full
   text of `AGENTS.md`, and `PR_BODY` if known. The sub-agent acts as three
   specialists and returns findings as
   `[{agent, severity: blocker|warning|info, file?, line?, detail}]`:

   - **ReviewAgent** — per `AGENTS.md`: comments and docs in English; no
     vibecoding (whitespace-only churn, dead commented blocks, unscoped
     TODOs, files outside the declared PR scope); `README.md` and
     `README_ES.md` updated together when either changes.
   - **SecurityAgent** — no `shell=True`; every spawned process must run in
     its own process group and register its pgid; no secrets or `QG_*`
     values outside env vars / `.env.example`.
   - **QAAgent** — tests added or updated for behavior changes; the suite
     from step 3 must be green; new code paths that spawn processes must
     have a cleanup assertion.

6. Emit the verdict **in Spanish** using the contractual format:

   ```markdown
   ## Validación funcional

   **Veredicto:** ✅ Aprobado | ❌ Cambios requeridos

   - ✅/❌ **check** — detalle

   **Result:** <resumen>
   **Note:** <nota>
   ```

   `Aprobado` requires: green tests, no blocker finding, and README
   alignment when docs changed. List every blocker with file and line.
