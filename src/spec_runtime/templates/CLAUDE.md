# Claude Code – Agent-Specific Notes

> Read `AGENTS.md` first. It is the project's source of truth; this file is
> strictly additive and must not contradict or duplicate it.

## Orchestrated sessions

When `spec` launches a non-interactive Claude session, the execution backend,
generated settings, and launch arguments define its permissions. Do not try to
loosen or work around those controls from inside the session. If a required
host tool or credential is unavailable, report `needs-input` or `blocked` as
described in `AGENTS.md`.

Never extract a user's GitHub token into shell commands or replace `gh` with a
hand-written authenticated `curl` request. Forge publication and merge remain
host-orchestrator responsibilities during an implementation run.

## Local settings

- `.claude/settings.json` contains project-level settings and may be committed.
- `.claude/settings.local.json` contains personal overrides and should remain
  gitignored.
