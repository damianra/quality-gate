---
description: validate a pull request like an autonomous reviewer sub-agent
---

# PR review sub-agent

Locally validates the current branch (or an open PR) before merge. The
final verdict is reported to the user **in Spanish**; everything written
to files or commits stays **in English** per `AGENTS.md`.

The gate is a **single functional validation** performed by one delegated
sub-agent (`devin` CLI, model `swe-2-high`) with a fresh context: the
parent agent gathers the raw evidence (diff, test results, scanner
output), hands it to the sub-agent, and only formats its findings into
the final verdict. Do not review inline in the parent agent.

The three-specialist Review/Security/QA review belongs to the
orchestrator project itself (`.github/workflows/pr-review.yml`) and is
exercised once the system is deployed — it is not part of this gate.

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

5. **Delegate to the review sub-agent** — one `devin` CLI call, free
   model:
   `devin --print --model swe-2-high --permission-mode auto
   --respect-workspace-trust false --verbosity 0 --prompt-file <file>`.
   The prompt file is self-contained: reviewer role + output contract,
   the full text of `AGENTS.md`, the PR number/title/body (or branch
   scope), the step-2 diff, the step-3 test output and the step-4 grep
   hits. The sub-agent acts as a **single reviewer** and returns findings
   as `[{severity: blocker|warning|info, file?, line?, detail}]` plus a
   `VERDICT: approve|changes_required` line.

   The reviewer applies the `AGENTS.md` checklist as one pass: docs and
   comments in English, conventional commits, `README.md`/`README_ES.md`
   mirror alignment when either changes, no `shell=True`, pgid-tracked
   subprocesses, no secrets outside env vars, green suite, and no
   vibecoding leftovers.

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
