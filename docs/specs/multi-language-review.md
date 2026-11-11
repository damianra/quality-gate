# Spec — language/ecosystem-agnostic review

**Status:** accepted — implementation split across two PRs
(`feat/multi-lang-qa`, `feat/multi-lang-signals`).
**Scope:** amends SPEC.md FR-O2 (specialist selection), FR-O3
(debug-leftover + scope-hint findings), FR-O4 (dependency finding
wording), FR-O5 (QAAgent validators), FR-O12 (blast-radius scoping)
and §6.2 (signals schema). Requirement IDs `FR-L*` are stable; tests
and PRs reference them.

## 1. Motivation

quality-gate was originally scoped to Expo/React Native repos and its
deterministic layer still carries that bias: the QA specialist only
activates on JS/TS file extensions and only knows `tsc`/`node`
validators, the debug-leftover and new-dependency signals only match
JS artifacts (`console.log`, `package.json`), scope hints recognize a
fixed extension list, and dependent discovery only parses JS/Python
import specifiers. Reviewing a NestJS backend works by accident (it is
TypeScript); a Python, Go or Rust service gets a degraded review.

This spec makes the pipeline ecosystem-agnostic: signals, validators,
scope hints and dependent discovery cover the mainstream languages,
while activation stays strictly code-gated — documentation, config and
data files never trigger QA on their own.

## 2. Requirements

- **FR-L1 Multi-language QA activation.** `choose_agents` activates
  `qa` when any changed file's extension belongs to the configurable
  set `QG_QA_CODE_EXTS` (comma-separated env override, `config.py`
  default): `.ts .tsx .js .jsx .mjs .cjs .py .go .rs .java .kt .rb
  .php .cs .swift .c .cc .cpp .h .hpp`. Files outside the set — docs,
  configs, lockfiles, data — never activate QA by themselves. A
  docs-only PR still runs `review` (and `security` on its own
  signals); `qa` requires at least one code file. Amends FR-O2.

- **FR-L2 Pluggable QA validators.** `_qa_commands` builds the command
  list from the ecosystems present in the changed files, following the
  `_audit_command` detector pattern (`shutil.which` + manifest
  probes):

  | Ecosystem | Detector | Command |
  |---|---|---|
  | TS | `node_modules/.bin/tsc` + `tsconfig.json` | `tsc --noEmit` |
  | JS | `node` on PATH | `node --check <file>` (≤20 changed files) |
  | Python | `python3`/`python` on PATH | `python -c` stdlib `compile()`
  syntax check on the changed `.py` files (no bytecode artifacts) |
  | Go | `go.mod` at repo root + `go` on PATH | `go vet ./...` |
  | Rust | `Cargo.toml` at root + `cargo` on PATH | `cargo check` |

  Commands from all matching ecosystems are appended (a polyglot PR
  gets several validators). No detector matches → the existing
  "No validators available" info finding; the LLM enrichment pass
  still runs regardless. The spawn contract is unchanged
  (INV-1/INV-2): argv list via `spawn_process`, every `pgid`
  registered in `active_pgids`, non-zero exit = blocker, timeout =
  warning, exit 127 = info + stop. Amends FR-O5.

  The CI orphan check (`.github/workflows/pr-review.yml` `pgrep`
  pattern) is widened in the same change to cover the new spawned
  process signatures (`compile(fh.read`, `go vet`, `cargo check`),
  and the INV-2 / README wording becomes "validator/scanner
  processes" instead of a fixed command list.

- **FR-L3 Multi-language debug-leftover signal.** `debug_leftovers`
  matches unambiguous debug statements across languages: JS
  `console.(log|debug)(` and `debugger`; Python `breakpoint(` and
  `pdb.set_trace`; Ruby `binding.pry`/`byebug`; Rust `dbg!(`.
  `print(`/`fmt.Println(` and other ordinary output calls are
  deliberately excluded — too false-positive prone. Amends §6.2 and
  FR-O3's "debug leftovers" finding.

- **FR-L4 Manifest-agnostic new-dependency signal.**
  `new_dependencies` entries become `{name, manifest}` dicts and are
  collected from every supported manifest, not only `package.json`:

  | Manifest | Detection |
  |---|---|
  | `package.json` | existing `"name": "version"` parse (unchanged) |
  | `requirements*.txt` | added `name[==|>=|~=|<=|>]ver` lines |
  | `pyproject.toml` | added quoted PEP-508 entries carrying a version
  specifier or marker (`"pkg>=1"`, `"pkg @ …"`, `"pkg; …"`) — bare
  quoted strings could be keyword/classifier arrays and don't count |
  | `go.mod` | added `require <module> <version>` lines |

  SecurityAgent's "New dependency" finding reports the real manifest
  path in `file` instead of the hardcoded `package.json`. Amends
  FR-O4 and §6.2. The FR-O24 vulnerability audit is already
  ecosystem-agnostic (`_DEP_MANIFESTS`) and unchanged.

- **FR-L5 Language-agnostic scope hints.** `_HINT_RE` is replaced by a
  generic path-token matcher: any `[\w./-]+\.[A-Za-z0-9]{1,6}` token
  in the PR body counts as a file hint (previously a fixed extension
  list), and any slash-containing path token counts as a directory
  hint covering every file beneath it. Letter lookaheads — one in
  the token, one inside the suffix — keep version tokens
  (`1.2.3`, `v1.2.3`) out. Doc paths remain always in-scope
  (`_DOC_PATH_RE` unchanged).
  Amends FR-O3's `files_outside_hint` finding and §6.2.

- **FR-L6 Multi-language dependent discovery.** `find_dependents`
  scans additional suffixes `.go .rs .java .rb .php .cs` and
  recognizes per-language import specifiers: Go quoted
  `import "pkg/mod"`, Rust `use`/`mod`, Java `import a.b.C`, Ruby
  `require`, C# `using`, PHP `use`/`require`. `_imports_target` is
  unchanged — it already handles `/` and `.` separators. Scan caps
  (`_MAX_SCAN_FILES`, `QG_SCOPE_MAX_FILES`, `_SKIP_DIRS`) are
  unchanged. Amends FR-O12.

  Known heuristic limits (accepted): Rust grouped imports
  `use a::{b, c}`, PHP `use function`/`use const`, and Go blank/dot
  imports (`_ "p"`, `. "p"`) are not matched; Go dependents resolve
  at package-dir granularity, not per file.

## 3. Contracts

### 3.1 Signals schema amendment (SPEC.md §6.2)

```text
new_dependencies[{name: str, manifest: str}]
```

All other signal shapes are unchanged; `debug_leftovers[]` remains a
list of matched added lines.

### 3.2 `QG_QA_CODE_EXTS`

Comma-separated extension list (with or without leading dot), env
`QG_QA_CODE_EXTS`, default per FR-L1. Parsed once in `config.py`
like the other `QG_*` settings.

## 4. Non-functional

- **No new dependencies** — detection is `shutil.which` and stdlib
  only; validators are external binaries used only when present.
- **Best-effort validators** — a missing toolchain degrades to the
  existing info finding and never blocks a PR by itself.
- **False-positive policy** — debug patterns are limited to
  statements that unambiguously mark a debugging session; ordinary
  output calls stay out.
- **Bounded scan** — the extra dependent-discovery suffixes reuse the
  existing file/budget caps; worst-case cost is one extra `rglob`
  pass over the same limits.
- **No LLM contract change** — specialist prompts, the output
  contract and the findings schema are untouched; this spec is
  deterministic-layer only.
