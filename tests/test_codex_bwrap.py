from __future__ import annotations

import pytest

from spec_runtime.codex_bwrap import bubblewrap_argv


@pytest.mark.parametrize("home", [
    "/workspace/provider-homes/codex",
    "/workspace/outbox/specbutler-sandbox-fixture/codex",
])
def test_redirects_only_helper_command_preserving_the_entire_boundary(home):
    helper = home + "/tmp/arg0/codex-arg0session/codex-linux-sandbox"
    policy = ["--unshare-user", "--ro-bind", "/", "/", "--tmpfs", home]
    arguments = [*policy, "--", helper, "--sandbox-policy", "unchanged", helper]
    assert bubblewrap_argv(arguments) == [
        "/usr/bin/bwrap", *policy, "--", "/usr/local/bin/codex-linux-sandbox",
        "--sandbox-policy", "unchanged", helper,
    ]
    assert arguments[len(policy) + 1] == helper


@pytest.mark.parametrize("arguments", [
    [], ["--version"], ["--"], ["--", "bash", "-c", "true"],
    ["--", "/elsewhere/codex-linux-sandbox"],
    ["--", "/workspace/provider-homes/codex/tmp/arg0/codex-arg0x/../codex-linux-sandbox"],
    ["--", "/workspace/provider-homes/codex/tmp/arg0/codex-arg0x/apply_patch"],
    ["--ro-bind", "/workspace/provider-homes/codex/tmp/arg0/codex-arg0x/codex-linux-sandbox",
     "/helper", "--", "bash"],
])
def test_other_commands_and_mount_arguments_pass_through(arguments):
    assert bubblewrap_argv(arguments) == ["/usr/bin/bwrap", *arguments]
