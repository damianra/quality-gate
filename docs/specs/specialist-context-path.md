# Spec — specialist context path and deterministic inspection

**Status:** accepted — implementation split across two PRs
(`feat/specialist-context`, `feat/inspection-signals`).
**Scope:** amends SPEC.md FR-O7 (synthesizer), FR-O23 (specialist
prompt evidence), §6.2 (signals schema) and §7 (configuration
surface). Requirement IDs `FR-C*` are stable; tests and PRs
reference them.

## 1. Motivation

A functional comparison run on `metabuilder-orchestrator#177` showed
the verdict comment is dominated by noisy deterministic findings
(six identical `secret_candidates` warnings on benign test fixtures)
while the LLM specialists produced only generic notes ("coverage not
verified"). An independent three-role review with the same diff found
concrete defects the pipeline missed: an unchecked `remote remove` →
`remote add` sequence that can leave the repo without a remote, an
untested `!add.ok` failure branch, and a combined-stderr payload that
consumers truncate from the wrong end.

The root cause is not model intelligence alone — it is the evidence
path. `_llm_user_message` (FR-O23) ships the diff plus changed-file
contents, but three blocks of evidence the prompts already demand are
never delivered:

- `pr_body` is stored in graph state and never sent, so the
  ReviewAgent mandate "files changed vs. files claimed in the PR
  body" is unenforceable.
- The target repo's `AGENTS.md` is never sent, so "evaluate the PR
  against the repo's AGENTS.md rules" is unenforceable.
- `related_files` ships names only ("dependents, not shown"), so the
  model cannot inspect the callers that consume a changed function.

On top of that the signals layer reports raw hits
(`secret_candidates[{file,line}]`) but no *inspection points*: new
error-handling branches, destructive→recreative sequences and the
source↔test coverage map are all derivable deterministically and are
exactly the places where a small local model needs its attention
directed.

This spec closes the context gaps, adds three deterministic
inspection signals, and turns them into a numbered checklist each
specialist must evaluate — "judge these concrete points" instead of
"search the diff freely". It also collapses identical findings in the
synthesizer so repeated signal hits stop dominating the comment.

## 2. Requirements

### Context completeness (PR `feat/specialist-context`)

- **FR-C1 PR body in specialist evidence.** `_llm_user_message`
  includes `state.pr_body` in a `## PR body` section, capped by
  `QG_PROMPT_MAX_BODY_CHARS` (default 4000). Empty bodies render as
  `(empty)`. The body is untrusted data and ships inside the FR-O25
  sentinels like every other block. Amends FR-O23.

- **FR-C2 Target-repo `AGENTS.md` in evidence.** When the worktree
  root contains `AGENTS.md` (or `CLAUDE.md` fallback), its content is
  shipped in a `## Repo rules` section, capped by
  `QG_PROMPT_MAX_AGENTS_CHARS` (default 4000). Gathered once by
  `build_pr_context` (FR-O12) as `agents_doc`. Untrusted data —
  sentinel-fenced and neutralized per FR-O25. Amends FR-O23/FR-O12.

- **FR-C3 Dependent excerpts.** `build_pr_context` attaches, for up
  to `QG_RELATED_HEAD_FILES` (default 5) entries of `related_files`,
  the first `QG_RELATED_HEAD_CHARS` (default 1500) characters of each
  file as `related_heads[{path, head}]`. The user message renders
  them under `## Related file excerpts`; remaining dependents stay
  names-only. All excerpts draw from the shared prompt budget after
  diff and changed-file contents. Amends FR-O12/FR-O23.

- **FR-C4 Role-tailored evidence.** `_llm_user_message` is split into
  a shared core (PR id/title, `## PR body`, `## Deterministic
  signals`, `## Changed files`, `## Diff`) plus role-specific
  sections, so each specialist receives focused evidence instead of
  one identical blob:
  - `review`: `## Repo rules` (FR-C2), scope hints, file contents.
  - `security`: `secret_candidates`/`config_churn`/
    `new_dependencies` details, manifest file contents, dependency
    audit output when present (FR-O24).
  - `qa`: test-mapping and untested-paths signals (FR-C6/C7),
    validator results from `processes`, test file contents.
  The per-block caps of FR-O23 keep applying; every role message
  stays within the same total budget shape so `num_ctx` planning is
  unchanged.

- **FR-C5 Role playbooks.** `REVIEW_PROMPT`/`SECURITY_PROMPT`/
  `QA_PROMPT` move to a dedicated `prompts.py` module and expand into
  per-role playbooks: what to verify, a severity rubric (blocker =
  real defect only; missing tests/style ≤ warning), and the
  instruction to evaluate the checklist items individually (FR-C9).
  The output contract (`_LLM_OUTPUT_CONTRACT`) is unchanged. No
  runtime file I/O — playbooks are importable constants.

### Deterministic inspection (PR `feat/inspection-signals`)

- **FR-C6 `related_tests` signal.** `analyze_diff` (or a scope.py
  helper it calls) maps each changed code file to test files present
  in the repo or the diff, via naming conventions across ecosystems:
  `foo.test.*`/`foo.spec.*`/`__tests__/foo.*` (JS/TS), `test_foo.py`/
  `foo_test.py`/`tests/test_foo.py` (Python), `foo_test.go` (Go),
  `foo_test.rs`/`tests/foo.rs` (Rust), `FooTest|TestFoo|FooTests`
  (Java/Kotlin/C#), `foo_spec.rb`/`spec/` (Ruby), `*Test.php`/
  `tests/` (PHP). Emitted as
  `related_tests[{source, tests: [str], test_changed: bool}]` where
  `test_changed` marks test files that are also in the PR diff.
  Adds to §6.2.

- **FR-C7 `untested_paths` signal.** `analyze_diff` scans added lines
  in code files (`QG_QA_CODE_EXTS`) for new error-handling branches:
  `if\s*\(!\w+\.ok`/`catch`/`throw`/`\.reject`/`Result\.Err`
  (JS/TS), `except`/`raise`/`sys\.exit` (Python), `if err != nil`/
  `return err`/`panic\(` (Go), `Err\(`/`panic!` (Rust),
  `raise`/`rescue` (Ruby), `throw`/`catch` (Java/Kotlin/C#/PHP).
  Entries `{file, line, kind, covered}`; `covered` is `true` only
  when a `related_tests` entry for that file exists AND its
  `test_changed` is `true` — a heuristic, not a coverage guarantee.
  Uncovered entries are QA checklist items per FR-C9. Adds to §6.2.

- **FR-C8 `state_mutations` signal.** `analyze_diff` detects
  destructive→recreative pairs among added lines of the same file:
  (`remove`,`add`), (`drop`,`create`), (`delete`,`insert`),
  (`unlink`,`write|create`), (`rm `,`mkdir|write`). Emitted as
  `{file, remove_line, add_line, pair}` — flagging sequences where
  the intermediate failure path destroys state (e.g. `git remote
  remove origin` followed by `remote add`): exactly the pattern that
  leaves no rollback if the second step fails. Known limit: both
  members of the pair must be added lines — a pre-existing
  destructive call paired with a newly added recreative one is not
  matched. Adds to §6.2.

- **FR-C9 Inspection checklist in the user message.** Each role's
  user message gains a `## Inspection checklist` section built from
  FR-C6..C8 plus existing signals, each item tagged with the role it
  belongs to:
  `1. [qa] finalizer.js:56 adds error branch 'if (!add.ok)' — is the
  failure path consistent and the error propagated?`
  `2. [security] tests/x.test.js:32 'token: t' — real credential or
  benign fixture?`
  `3. [review] finalizer.js:54-55 'remote remove'→'remote add' —
  is the intermediate failure handled?`
  A role receives only its own items, capped at
  `QG_CHECKLIST_MAX_ITEMS` (default 12) per role message — the cap
  applies after role filtering so one role's volume cannot starve
  another's. When the filtered list is empty the section is omitted.
  The playbook (FR-C5) instructs the model to evaluate every item,
  not just scan the diff.

- **FR-C10 Synthesizer dedup.** Before building the checks list,
  `synthesizer_node` collapses findings with identical
  `(agent, title, file)` into one, keeping the highest severity and
  appending the occurrence count to the detail
  (`— N similar findings`). Applies to deterministic and LLM
  findings alike; the six identical secret warnings on the same test
  file become one. Amends FR-O7.

## 3. Contracts

### 3.1 Signals schema amendment (SPEC.md §6.2)

```text
related_tests[{source, tests: [str], test_changed: bool}]
untested_paths[{file, line, kind, covered}]
state_mutations[{file, remove_line, add_line, pair}]
```

All other signal shapes are unchanged.

### 3.2 `pr_context` additions (SPEC.md §6.4 via FR-O12)

```text
agents_doc: str | None      # target-repo AGENTS.md/CLAUDE.md (FR-C2)
related_heads: [{path, head}]  # dependent excerpts (FR-C3)
```

### 3.3 New `QG_*` settings (SPEC.md §7)

| Setting | Default | Purpose |
|---|---|---|
| `QG_PROMPT_MAX_BODY_CHARS` | 4000 | `## PR body` cap (FR-C1) |
| `QG_PROMPT_MAX_AGENTS_CHARS` | 4000 | `## Repo rules` cap (FR-C2) |
| `QG_RELATED_HEAD_FILES` | 5 | dependents excerpted (FR-C3) |
| `QG_RELATED_HEAD_CHARS` | 1500 | chars per dependent head (FR-C3) |
| `QG_CHECKLIST_MAX_ITEMS` | 12 | checklist item cap (FR-C9) |

### 3.4 Finding schema (unchanged)

Findings keep `{agent, severity, title, detail, file}`; dedup only
merges identical triples before rendering.

## 4. Non-functional

- **Single-shot preserved** — no tool-calling loop; the checklist is
  generated deterministically and the model still emits one JSON
  array under the existing contract.
- **Bounded** — every new evidence block has a `QG_*` cap; role
  messages stay inside the existing budget model so the deployed
  `num_ctx` keeps governing worst-case prompt size.
- **Pure-stdlib signals** — FR-C6..C8 are regex/name-matching in
  `tools.py`/`scope.py`; no external binaries, no network.
- **Untrusted data** — `pr_body` and `AGENTS.md` enter the sentinel
  region like all other evidence; `_neutralize_sentinels` applies
  (FR-O25). A hostile `AGENTS.md` or PR body cannot inject
  instructions.
- **Heuristic honesty** — `untested_paths.covered` is a naming/
  presence heuristic, not real coverage; docs and the checklist
  wording say "evaluate", not "verified". Same documented limits
  style as FR-L6.
- **Model portability** — nothing here assumes a provider; better
  results with stronger models ride entirely on existing FR-O13/
  FR-O15 role config.
