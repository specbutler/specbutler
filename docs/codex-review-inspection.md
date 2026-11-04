# Codex review inspection

The review adapter registers only `review_inspection` when a pinned checkout is
available. It starts the host installation's Python and server code, rather than
code from the reviewed checkout. The reviewer still has no shell or unified
execution tool; `CODEX_REVIEW_CHECKOUT_SHELL` remains false. Without a checkout,
review retains its previous tool-free boundary.

The MCP server supports `read_file`, `list_dir`, `glob`, literal-text `grep`, and
`git_show`. Paths are relative to the launch root. Absolute paths, parent
traversals, resolved escapes, and operator/provider credential paths are refused.
Directory traversal skips Git metadata and linked directories. Reads consume at
most 64 KiB of file data; listing/search responses contain at most 2,000 entries
and 64 KiB of entry data. A `truncated` flag reports omitted content, including
files whose unread tail could contain grep matches.

`git_show` accepts `operation` (`show`, `log`, or `diff`), `revision` (default
`HEAD`), and explicit `paths` identifying current regular files. It deliberately
refuses arbitrary Git options, pathspec magic, directories, and unrestricted
history queries. Git output is limited to 64 KiB, execution to ten seconds, and
logs to fifty commits. External diffs, textconv, signature verification,
filesystem monitors, hooks, submodule recursion, lazy fetching, and transport
protocols are disabled. The child receives no inherited Git configuration
environment; system and global configuration are disabled. The checkout remains
the explicit work tree even if local config names another directory.

## CLI verification

Verified with **codex-cli 0.164.0** on Linux, 2026-11-03. The opt-in smoke test
starts the real `codex exec` with the production review argv and stdio MCP
server. A local mock Responses endpoint requests `read_file`; assertions check
the actual completed MCP tool event contains the fixture file contents. The
mock endpoint replaces only the model provider, so no authentication or remote
network service is needed. Shell, unified execution, and the code-mode host
remain disabled throughout this check.

Run it with:

```bash
SPEC_CODEX_INSPECTION_SMOKE=1 .venv/bin/python -m pytest tests/test_review_inspection.py -q
```

The test records the installed CLI version as a pytest property. It is skipped
by default and uses the suite's local-socket capability probe.
