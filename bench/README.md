# bench/ — review-quality golden set (FR-O31)

An executable answer to *"does the gate actually catch what it claims
to?"* — the missing piece between unit tests (which verify plumbing)
and trusting the verdict on a real PR.

`run.py` replays a corpus of synthetic PRs through the **real
deterministic stages** — `analyze_diff` signals plus the actual
`review` / `security` / `qa` specialist nodes, including the FR-O29
scanner toolchain inside the FR-O28 sandbox — and checks the produced
findings against per-case expectations. No GitHub API, no LLM.

## Why it exists

- **Regression gate for detection quality.** A ruleset edit, a parser
  change or a severity-mapping tweak can silently drop a detection.
  The corpus fails loudly when that happens.
- **Adversarial coverage.** Several cases replay the suppression and
  evasion attacks FR-O29/FR-O30 defend against (repo-shipped
  `.gitleaksignore` / `trivy.yaml`, inline `# nosem`, prompt-injection
  lines). If a future refactor reopens one of those holes, this is
  where it surfaces.
- **False-positive measurement.** `clean-*` cases assert that tidy PRs
  produce nothing above a severity cap — detection without precision
  is noise.
- **Model/ruleset comparison.** `--include-llm` runs let you compare
  how an LLM provider or a new rule changes outcomes on a fixed corpus.

It already caught a real bug on first run: `_qa_commands` passed the
*host's* absolute interpreter path into the sandbox container, where
it does not exist — the Python compile validator silently failed on
every sandboxed PR. Fixed by shipping bare executable names.

## Running it

```bash
# full corpus (needs podman/docker + the qg-sandbox image for
# scanner cases; they skip cleanly without it)
uv run python bench/run.py

# one case, verbose findings
uv run python bench/run.py --case secret-new-file --json /tmp/r.json

# parallel + reports for CI
uv run python bench/run.py -j 4 --json report.json --md report.md
```

Exit code is non-zero when any executed case fails — wire it into a
*periodic* CI job (weekly / on ruleset or scanner changes), not the
per-PR gate: each sandbox case costs ~2 min of container work.

`--include-llm` leaves `QG_LLM_*` in the environment so a configured
provider enriches findings. Those runs are flagged `llm: true` and are
for comparison/observation only — LLM output is non-deterministic, so
pass/fail still evaluates the deterministic findings.

## Authoring a case

Each `cases/*.yaml` is one synthetic PR:

```yaml
id: my-case                       # unique
title: what this PR is testing
tags: [security, gitleaks]        # feeds the per-tag detection table
requires: sandbox                 # optional — skip w/o container runtime
files:                            # the repo tree AT PR HEAD; these are
  app/config.py: |                # the "changed" files
    TOKEN = "ghp_..."
extra_files:                      # on disk but NOT changed — pre-existing
  .semgrepignore: |               # content (counter-scan surfaces)
    *.py
diff_text: |                      # optional; when omitted the harness
  ...                             # synthesizes "all lines added" hunks
pr_body: "..."
expect:
  signals: [secret_candidates]    # analyze_diff keys that must be truthy
  findings:                       # each must match ≥1 produced finding
    - agent: security             #   fields AND-match; `match` is a
      severity: blocker           #   case-insensitive substring over
      match: committed secret     #   title+detail+file
  no_unexpected_above: warning    # optional FP cap: fail if any
                                  # unmatched finding reaches this level
```

Guidelines:

- **Assert detection, not wording** — `match` on stable rule ids
  (`python-eval-exec`) or title stems (`committed secret`), not full
  sentences.
- **Cover your rule.** FR-O33: every new Semgrep rule ships with a
  case proving it fires, plus (ideally) a clean case bounding its FP
  cost.
- **Model the adversary, not just the bug.** Suppression/evasion
  cases matter as much as detection cases — a scanner that can be
  muted is worth less than one that cannot.
- `files` must contain the *post-PR* content; when `diff_text` is
  omitted every line counts as added (matching what gitleaks' added-
  line filter sees).

## Report format

`--json` produces `{summary, sandbox, include_llm, results[]}` — per
case: `status` (`pass`/`fail`/`skipped`), `missing` expectations,
`unexpected` findings above the FP cap, `missing_signals`, the produced
`findings` (agent/severity/title/file) and `duration_s`. `--md` renders
a table plus a per-tag detection matrix — the artifact a security team
would review before trusting the gate.
