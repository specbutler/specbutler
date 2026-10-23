"""Global test configuration — isolate tests from the repo's .spec.toml."""

import os

# Point SPEC_CONFIG at a nonexistent path so tests use built-in defaults
# instead of picking up the repo's .spec.toml.
os.environ["SPEC_CONFIG"] = "/dev/null/nonexistent/.spec.toml"

# When the suite runs inside a spec worker/container (as the orchestrator's
# verify phase does), the ambient environment carries the completion-outbox
# handshake variable. If left set, `spec complete`/`cmd_complete` diverts every
# completion to the container outbox and returns early, so tests exercising the
# normal completion path see no recorded result (a spurious "false handshake").
# Neutralize it here so tests are hermetic regardless of where the suite runs;
# tests that need it set it explicitly via monkeypatch.
os.environ.pop("SPEC_COMPLETION_OUTBOX", None)

# The agent Git probe starts real provider sandboxes (`codex sandbox`, `srt`).
# Keep doctor and implement-launch tests hermetic; probe tests opt back in.
os.environ["SPEC_AGENT_GIT_PROBE"] = "0"
