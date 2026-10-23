# Codex – Agent-Specific Notes

> Codex reads `AGENTS.md` on startup. It is the project's source of truth; this
> file is strictly additive and must not contradict or duplicate it.

## Orchestrated sessions

`spec` launches Codex implementation sessions with workspace-write and network
access, interactive authoring with workspace-write and approval-gated network
access, and local review sessions read-only. Approval prompts are unavailable
in non-interactive runs: an escalation fails back to you, or goes to Codex's
model reviewer when `[execution.codex] auto_review` is on. These launch
settings, `[execution].safety_mode` and `[execution.network]`, and the selected
execution backend are the effective boundary.

Follow the provided workspace and implement-agent contract. If the requested
work cannot be completed within the available permissions, report
`needs-input` or `blocked` rather than changing sandbox policy.

## Local configuration

The `.codex/` directory is machine-local and must not be committed.
