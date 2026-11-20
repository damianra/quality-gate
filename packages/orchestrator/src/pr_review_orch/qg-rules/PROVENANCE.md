# qg-rules provenance (FR-O33)

License/provenance accounting for the vendored semgrep ruleset — the
ruleset ships inside the `qg-sandbox` image, so every rule's origin and
license compatibility is recorded here. Ports are **original
rewrites**, never copies of upstream rule sources.

Each rule's `metadata.source` names one family id below; the unit
suite (`test_ruleset.py`) enforces that every declared source exists
in this table.

| family | source project | reference | license | port method |
|--------|----------------|-----------|---------|-------------|
| qg | quality-gate (original) | — | repository license | original |
| bandit | PyCQA/bandit | 1.8.x rule set | Apache-2.0 | rewrite |
| gosec | securego/gosec | 2.x rule set | Apache-2.0 | rewrite |
| nodejsscan | ajinabraham/NodeJSScan | scanner checks | GPL-3.0 | clean-room |

## Port methods

- **original** — written for this repository; ships under the
  repository's license.
- **rewrite** — re-implemented as a semgrep pattern from the upstream
  check's documented behavior. Apache-2.0 sources permit derivative
  redistribution under their license with notice; the `ref:` metadata
  records the upstream check id (e.g. `B106`, `G201`).
- **clean-room** — written only from the vulnerability class / CWE
  description and public documentation of what the check catches,
  never from the GPL-licensed rule source. NodeJSScan is GPL-3.0, so
  its checks cannot be adapted directly inside the distributed image.

## Non-goals

- No ruleset is fetched from the semgrep registry or any other remote
  source at scan or build time — `qg-rules/` is the entire corpus.
