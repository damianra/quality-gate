# AGENTS.md

Conventions every contributor — human or agent — must follow in this repo.
The orchestrator's ReviewAgent also reads this file to score PRs.

## Language rules

- **Code, comments and documentation are always in English.** That covers
  docstrings, inline comments, commit messages, workflow files, Dockerfiles
  and READMEs.
- **User-facing responses are in Spanish, the PR verdict comment in
  English.** Chat answers and any text directed at the user are written
  in Spanish; the posted `## Functional validation` comment is English
  (SPEC.md §6.1).

## Commit conventions

Commits follow [Conventional Commits](https://www.conventionalcommits.org):
`<type>(<scope>): <imperative summary>` in English. Allowed types:
`feat`, `fix`, `docs`, `test`, `ci`, `build`, `chore`, `refactor`, `perf`,
`revert`. The scope is the package name (`mcp-server`, `orchestrator`)
when the change is package-local; cross-cutting commits omit it.

## Bilingual documentation

- `README.md` (English) and `README_ES.md` (Spanish) are mirrors: every
  change to one requires the equivalent change in the other, in the same PR.
  A reviewer must reject a PR that updates only one of them.
- Package-level docs (`packages/*/README.md`) are English-only.

## Process safety (non-negotiable)

- `shell=True` is forbidden; every subprocess is an argv list.
- Long-running commands spawn in a new process group
  (`start_new_session=True`) and their `pgid` is registered in
  `active_pgids` so the cleanup node can `killpg` it.
- `GITHUB_TOKEN` and `DATABASE_URL` come from env vars only — never in code,
  logs, commit messages or CLI arguments.

## Branching & review flow

- **No direct commits to `main`.** Every change — code, docs, config,
  this file — lands on a dedicated branch named after its change type:
  `feat/...`, `fix/...`, `docs/...`, `ci/...`, `refactor/...`,
  `chore/...`.
- **Every branch goes through the pr-review workflow** before merging —
  `.devin/workflows/pr-review.md`: a delegated fresh-context sub-agent
  reviews the gathered evidence and emits the verdict.
- **The branch is pushed and a PR opened** — even for small fixes —
  so the review evidence (diff, tests, scanner) is attached to a
  mergeable unit. Merging to `main` requires the review verdict to be
  clean.

## PR body conventions

The review bot and human reviewers read the PR body as the declared
scope of the change:

- **Declare the affected paths.** List the touched dirs/files in the
  body (e.g. a `## Scope` section). The `files_outside_hint` signal
  flags every diff path not covered by the body's path hints — a
  partial list flags the rest, an empty list flags nothing.
- **Call out sensitive-config changes.** `.env*`, `compose.yaml`,
  `.github/workflows/*`, `Dockerfile`s and `deploy/*` are on the
  security watchlist — state briefly in the body why each change is
  safe (no secrets, no new public surface).
- **Keep the PR focused.** Past ~600 changed lines the oversized-PR
  warning fires; prefer splitting docs/config/code into separate PRs
  unless they ship one feature together.

## PR review checklist

- [ ] Comments and documentation in English; user-facing text in Spanish
- [ ] Commit messages follow Conventional Commits (English, imperative)
- [ ] `README.md` and `README_ES.md` aligned
- [ ] No `shell=True`; spawned processes tracked via pgid
- [ ] No secrets outside environment variables
- [ ] `uv run pytest packages -q` passes
- [ ] No vibecoding leftovers: debug statements, dead commented code,
      unscoped TODOs, files outside the declared PR scope
- [ ] PR body declares the affected paths and calls out any
      sensitive-config change
- [ ] Focused diff (guide: consider splitting past ~600 changed lines)
