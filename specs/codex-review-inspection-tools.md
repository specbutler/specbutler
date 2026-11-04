---
id: codex-review-inspection-tools
area: backend
priority: 30
depends_on: []
description: Give the Codex reviewer read-only inspection tools for the review checkout, with no shell
---

# Codex Review Inspection Tools

## Goal

The Claude reviewer inspects the pinned review checkout with read-only `Read`,
`Glob` and `Grep` tools. The Codex reviewer has no inspection at all. codex-cli
0.164 removed `approval_policy="untrusted"`, so a Codex shell can't be limited to
inspection commands, and #67 turned the review shell off to keep the rule that
reviewers never execute pull-request code. Give Codex the same capability through
a small read-only MCP server, scoped to the checkout, with the shell still off.

## Acceptance Criteria

- [ ] **A read-only inspection server exists.** A stdio MCP server in
  `spec_runtime` exposes `read_file`, `list_dir`, `glob`, `grep` and `git_show`, all
  rooted at one directory passed at launch. It has no tools that write, execute
  arbitrary commands, or reach the network.
- [ ] **The root can't be escaped.** Paths are resolved, and anything outside the
  root (`..`, absolute paths, symlinks or junctions pointing out) is refused.
  Provider homes and the operator credential paths from
  `protected_operator_paths()` are refused even if they're under the root. File
  reads and grep output have size limits, and the server reports truncation.
- [ ] **`git_show` is bounded.** It runs only `git show`, `git log` or `git diff`
  with argument validation (revisions and paths, no options that execute programs
  such as `--ext-diff` or textconv), against the checkout, with `GIT_CONFIG_NOSYSTEM`
  and an empty global config.
- [ ] **Codex review uses it.** When a review checkout exists, the Codex review
  launch registers only this server (`--strict-config`, shell and unified exec still
  off) and passes the checkout as its root. `CODEX_REVIEW_CHECKOUT_SHELL` stays
  False.
- [ ] **Prompts match the capability.** The Codex review note and both review
  templates (`src/spec_runtime/templates/review.md` and `.github/prompts/review.md`,
  kept identical) say the Codex reviewer can inspect the checkout with these
  read-only tools, as the Claude reviewer can.
- [ ] **It works with the real CLI.** A smoke check (an opt-in test or a `spec
  doctor` probe) starts `codex exec` with the server and confirms a tool call
  returns file contents. The PR records the codex-cli version it was verified
  against.
- [ ] **Hermetic tests cover:** each tool on a fixture checkout; root escapes
  (`..`, absolute, symlink); protected-path refusal; size limits; `git_show`
  argument rejection; and the review launch argv (server registered, shell off).

## Out of Scope

- Restoring a Codex shell. Revisit if Codex adds a supported inspection-only command
  policy.
- Changing the Claude reviewer.

## Design Notes

- Codex review launch: `CodexAgent.build_review_command` and
  `CODEX_REVIEW_CHECKOUT_SHELL` in `src/spec_runtime/agent_adapter.py`. Review is
  currently MCP-free (`del prompt, mcp_config_path`).
- MCP rendering already exists for implement launches: `_render_codex_mcp_toml` and
  the `mcp_servers.<name>.*` overrides.
- Claude parity: the Claude review launch uses `--tools Read,Glob,Grep`,
  `--allowedTools Read,Glob,Grep` and `--strict-mcp-config`.
- Prompt note: `_codex_review_sandbox_prompt_note` in `orchestrator.py`.

## Agent Notes

Probe the installed codex-cli before relying on any flag or config key (see the
#55/#56 and #57/#67 entries in `docs/plans/dogfood-log.md`): earlier changes
shipped config values Codex rejected.
