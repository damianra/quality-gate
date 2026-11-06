# AGENTS.md

Conventions every contributor — human or agent — must follow in this repo.
The orchestrator's ReviewAgent also reads this file to score PRs.

## Language rules

- **Code, comments and documentation are always in English.** That covers
  docstrings, inline comments, commit messages, workflow files, Dockerfiles
  and READMEs.
- **User-facing responses are in Spanish.** Chat answers, PR review
  summaries and any text directed at the user must be written in Spanish.

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

## PR review checklist

- [ ] Comments and documentation in English; user-facing text in Spanish
- [ ] `README.md` and `README_ES.md` aligned
- [ ] No `shell=True`; spawned processes tracked via pgid
- [ ] No secrets outside environment variables
- [ ] `uv run pytest packages -q` passes
- [ ] No vibecoding leftovers: debug statements, dead commented code,
      unscoped TODOs, files outside the declared PR scope
