# Codex – Agent-Specific Notes

> Codex reads `AGENTS.md` on startup. It is the project's source of truth; this
> file is strictly additive and must not contradict or duplicate it.

## Orchestrated sessions

`spec` currently launches Codex implementation and authoring sessions with
workspace-write and network access, and local review sessions read-only.
Approval prompts are unavailable in non-interactive runs. These fixed launch
settings—and the selected execution backend—are the effective boundary;
`[execution].safety_mode` does not change them.

Follow the provided workspace and implement-agent contract. If the requested
work cannot be completed within the available permissions, report
`needs-input` or `blocked` rather than changing sandbox policy.

## Local configuration

The `.codex/` directory is machine-local and must not be committed.
