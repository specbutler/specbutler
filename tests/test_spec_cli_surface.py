"""Tests for the spec CLI surface, forge adapter, and agent adapter boundaries.

Covers the new public CLI entry point (``spec_runtime.cli``), the forge
adapter boundary (``spec_runtime.forge``), and the agent adapter boundary
(``spec_runtime.agent_adapter``).  All tests are pure-unit — no network,
no git repos, no subprocesses.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runtime.agent_adapter import (
    _AGENT_REGISTRY,
    AgentAdapter,
    ClaudeAgent,
    CodexAgent,
    HostAgentUnavailableError,
    _codex_git_metadata_dirs,
    get_agent_adapter,
    host_agent_unavailability_reason,
    register_agent_adapter,
    require_host_agent_available,
)
from spec_runtime.config import SpecConfigNotFoundError
from spec_runtime.forge import (
    AUTO_MERGE_ARM_TIMEOUT_SECONDS,
    ForgeAdapter,
    GitHubForge,
    PullRequest,
    PushResult,
    get_forge_adapter,
    set_forge_adapter,
)

# ---------------------------------------------------------------------------
# Forge adapter tests
# ---------------------------------------------------------------------------


class TestGitHubForgeFindPr:
    """Tests for GitHubForge.find_pr_for_branch — including cwd propagation."""

    def _make_forge(self, run_fn):
        return GitHubForge(run_fn=run_fn)

    def test_find_pr_passes_cwd_to_runner(self, tmp_path):
        """The cwd kwarg must reach the underlying subprocess runner."""
        captured = {}

        def spy_run(cmd, cwd=None, **kw):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        forge = self._make_forge(spy_run)
        forge.find_pr_for_branch("feature/x", cwd=tmp_path)

        assert captured["cwd"] == tmp_path
        assert "--head" in captured["cmd"]
        assert "feature/x" in captured["cmd"]

    def test_find_pr_returns_none_on_empty_list(self):
        def run_fn(cmd, cwd=None, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        forge = self._make_forge(run_fn)
        result = forge.find_pr_for_branch("no-pr-branch")
        assert result is None

    def test_find_pr_returns_pr_on_json_result(self):
        pr_json = json.dumps(
            [
                {
                    "number": 42,
                    "url": "https://github.com/org/repo/pull/42",
                    "headRefName": "feat/x",
                    "baseRefName": "master",
                    "title": "My PR",
                    "state": "OPEN",
                    "isDraft": False,
                }
            ]
        )

        def run_fn(cmd, cwd=None, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=pr_json, stderr="")

        forge = self._make_forge(run_fn)
        result = forge.find_pr_for_branch("feat/x")

        assert result is not None
        assert result.number == 42
        assert result.url == "https://github.com/org/repo/pull/42"
        assert result.head_branch == "feat/x"
        assert result.title == "My PR"
        assert result.is_draft is False

    def test_find_pr_returns_none_on_failure(self):
        def run_fn(cmd, cwd=None, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error")

        forge = self._make_forge(run_fn)
        result = forge.find_pr_for_branch("broken")
        assert result is None

    def test_find_pr_passes_base_branch(self):
        captured = {}

        def spy_run(cmd, cwd=None, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        forge = self._make_forge(spy_run)
        forge.find_pr_for_branch("feat/x", base_branch="develop")

        assert "--base" in captured["cmd"]
        idx = captured["cmd"].index("--base")
        assert captured["cmd"][idx + 1] == "develop"

    def test_find_pr_defaults_to_configured_base_branch(self):
        captured = {}

        def spy_run(cmd, cwd=None, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        forge = self._make_forge(spy_run)
        with patch("spec_runtime.forge.load_spec_runtime_config") as mock_load:
            mock_load.return_value = MagicMock(pr_base_branch="main")
            forge.find_pr_for_branch("feat/x")

        assert "--base" in captured["cmd"]
        idx = captured["cmd"].index("--base")
        assert captured["cmd"][idx + 1] == "main"


class TestGitHubForgePushBranch:
    def test_push_branch_passes_cwd(self, tmp_path):
        captured = {}

        def spy_run(cmd, cwd=None, **kw):
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        forge = GitHubForge(run_fn=spy_run)
        result = forge.push_branch("my-branch", cwd=tmp_path)

        assert result.ok is True
        assert captured["cwd"] == tmp_path

    def test_push_branch_returns_failure_on_error(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rejected")

        forge = GitHubForge(run_fn=run_fn)
        result = forge.push_branch("my-branch", cwd=tmp_path)

        assert result.ok is False
        assert "rejected" in result.message


class TestGitHubForgeCreatePr:
    def test_create_pr_returns_pr(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="https://github.com/org/repo/pull/99\n",
                stderr="",
            )

        forge = GitHubForge(run_fn=run_fn)
        pr = forge.create_pr(
            title="Test PR",
            body="body",
            head="feat/x",
            base="master",
            cwd=tmp_path,
        )

        assert pr.number == 99
        assert pr.head_branch == "feat/x"

    def test_create_pr_raises_on_failure(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fail")

        forge = GitHubForge(run_fn=run_fn)
        with pytest.raises(RuntimeError, match="fail"):
            forge.create_pr(
                title="Test PR",
                body="body",
                head="feat/x",
                base="master",
                cwd=tmp_path,
            )


class TestGitHubForgeProtocol:
    def test_github_forge_implements_forge_adapter(self):
        """GitHubForge must satisfy the ForgeAdapter protocol."""
        assert isinstance(GitHubForge(), ForgeAdapter)

    def test_pinned_repo_slug_is_used_for_every_gh_call(self, tmp_path):
        captured = {}

        def run_fn(cmd, cwd=None, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        forge = GitHubForge(run_fn=run_fn, repo_slug="specbutler/specbutler")

        assert forge.get_repo_slug(cwd=tmp_path) == "specbutler/specbutler"
        assert forge.get_required_checks(12, cwd=tmp_path) == []
        assert captured["cmd"][:3] == ["gh", "pr", "checks"]
        assert captured["env"]["GH_REPO"] == "specbutler/specbutler"

    def test_required_checks_treats_gh_no_required_prose_as_empty(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):  # noqa: ARG001
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="no required checks reported on the 'code/example' branch\n",
            )

        checks = GitHubForge(run_fn=run_fn).get_required_checks(12, cwd=tmp_path)

        assert checks == []

    def test_mark_pr_ready_uses_gh_pr_ready(self, tmp_path):
        captured = {}

        def run_fn(cmd, cwd=None, **kw):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        forge = GitHubForge(run_fn=run_fn)

        assert forge.mark_pr_ready(12, cwd=tmp_path) is True
        assert captured["cmd"] == ["gh", "pr", "ready", "12"]
        assert captured["cwd"] == tmp_path

    def test_mark_pr_draft_uses_gh_pr_ready_undo(self, tmp_path):
        captured = {}

        def run_fn(cmd, cwd=None, **kw):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        forge = GitHubForge(run_fn=run_fn)

        assert forge.mark_pr_draft(12, cwd=tmp_path) is True
        assert captured["cmd"] == ["gh", "pr", "ready", "12", "--undo"]
        assert captured["cwd"] == tmp_path

    def test_merge_pr_can_match_expected_head(self, tmp_path):
        calls = []

        def run_fn(cmd, cwd=None, **kw):
            calls.append((cmd, cwd, kw))
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"state":"MERGED","headRefOid":"abc123","autoMergeRequest":null}',
                stderr="",
            )

        forge = GitHubForge(run_fn=run_fn)

        result = forge.merge_pr(12, method="squash", auto=True, expected_head_sha="abc123", cwd=tmp_path)

        assert result.ok is True
        assert calls[0][0] == [
            "gh",
            "pr",
            "merge",
            "12",
            "--squash",
            "--auto",
            "--match-head-commit",
            "abc123",
        ]
        assert calls[0][1] == tmp_path
        assert calls[0][2]["timeout"] == AUTO_MERGE_ARM_TIMEOUT_SECONDS
        assert calls[1][0] == [
            "gh",
            "pr",
            "view",
            "12",
            "--json",
            "state,autoMergeRequest,headRefOid",
        ]

    def test_merge_pr_auto_success_without_recorded_request_falls_back(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):  # noqa: ARG001
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"state":"OPEN","headRefOid":"abc123","autoMergeRequest":null}',
                stderr="",
            )

        result = GitHubForge(run_fn=run_fn).merge_pr(
            12,
            auto=True,
            expected_head_sha="abc123",
            cwd=tmp_path,
        )

        assert result.ok is False
        assert "auto-merge is not enabled" in result.message
        assert "no auto-merge request" in result.message

    def test_merge_pr_auto_timeout_returns_after_confirming_auto_merge_is_armed(self, tmp_path):
        calls = []

        def run_fn(cmd, cwd=None, **kw):
            calls.append((cmd, cwd, kw))
            if cmd[:3] == ["gh", "pr", "merge"]:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            assert cmd == [
                "gh",
                "pr",
                "view",
                "12",
                "--json",
                "state,autoMergeRequest,headRefOid",
            ]
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    '{"state":"OPEN","headRefOid":"abc123",'
                    '"autoMergeRequest":{"enabledAt":"2026-08-18T01:00:00Z"}}'
                ),
                stderr="",
            )

        result = GitHubForge(run_fn=run_fn).merge_pr(
            12,
            auto=True,
            expected_head_sha="abc123",
            cwd=tmp_path,
        )

        assert result.ok is True
        assert "auto-merge armed" in result.message
        assert calls[0][2]["timeout"] == AUTO_MERGE_ARM_TIMEOUT_SECONDS
        assert "timeout" not in calls[1][2]

    def test_merge_pr_auto_timeout_fails_closed_when_state_cannot_be_confirmed(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):  # noqa: ARG001
            if cmd[:3] == ["gh", "pr", "merge"]:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"state":"OPEN","autoMergeRequest":null}',
                stderr="",
            )

        result = GitHubForge(run_fn=run_fn).merge_pr(12, auto=True, cwd=tmp_path)

        assert result.ok is False
        assert "enablePullRequestAutoMerge timed out" in result.message

    def test_merge_pr_auto_timeout_fails_closed_when_expected_head_changed(self, tmp_path):
        def run_fn(cmd, cwd=None, **kw):  # noqa: ARG001
            if cmd[:3] == ["gh", "pr", "merge"]:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    '{"state":"OPEN","headRefOid":"changed",'
                    '"autoMergeRequest":{"enabledAt":"2026-08-18T01:00:00Z"}}'
                ),
                stderr="",
            )

        result = GitHubForge(run_fn=run_fn).merge_pr(
            12,
            auto=True,
            expected_head_sha="reviewed",
            cwd=tmp_path,
        )

        assert result.ok is False
        assert "resulting PR state could be confirmed" in result.message


class TestForgeFactory:
    def test_get_forge_adapter_returns_github_by_default(self):
        # Reset the global singleton so we test fresh
        import spec_runtime.forge as forge_mod

        old = forge_mod._FORGE_ADAPTER
        try:
            forge_mod._FORGE_ADAPTER = None
            adapter = get_forge_adapter()
            assert isinstance(adapter, GitHubForge)
        finally:
            forge_mod._FORGE_ADAPTER = old

    def test_set_forge_adapter_overrides(self):
        import spec_runtime.forge as forge_mod

        old = forge_mod._FORGE_ADAPTER
        try:
            mock_adapter = MagicMock(spec=ForgeAdapter)
            set_forge_adapter(mock_adapter)
            assert get_forge_adapter() is mock_adapter
        finally:
            forge_mod._FORGE_ADAPTER = old


# ---------------------------------------------------------------------------
# Agent adapter tests
# ---------------------------------------------------------------------------


class TestClaudeAgent:
    def test_native_macos_host_does_not_require_linux_sandbox_tools(self):
        assert host_agent_unavailability_reason(
            "claude",
            platform="darwin",
            which=lambda _name: None,
        ) == ""
        require_host_agent_available(
            "claude",
            platform="darwin",
            which=lambda _name: None,
        )

    def test_native_windows_host_launch_fails_closed_with_alternatives(self):
        reason = host_agent_unavailability_reason("claude", platform="win32")

        assert "not supported" in reason
        assert "Codex" in reason
        assert "WSL2" in reason
        assert "Linux container" in reason
        with pytest.raises(HostAgentUnavailableError, match="WSL2"):
            require_host_agent_available("claude", platform="win32")

    def test_codex_is_not_subject_to_claude_host_sandbox_policy(self):
        assert host_agent_unavailability_reason("codex", platform="win32") == ""
        require_host_agent_available("codex", platform="win32")

    def test_name(self):
        assert ClaudeAgent().name == "claude"

    def test_capabilities(self):
        caps = ClaudeAgent().capabilities
        assert caps.name == "claude"
        assert caps.supports_stream_json is True
        assert caps.supports_mcp is True

    def test_build_implement_command_basic(self, tmp_path):
        agent = ClaudeAgent()
        cmd = agent.build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
        )
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "--restricted" in cmd
        assert "--safe-mode" not in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert cmd[cmd.index("--settings") + 1] == str(
            tmp_path / ".claude" / "settings.local.json"
        )
        assert cmd[cmd.index("--setting-sources") + 1] == ""
        assert "--no-session-persistence" in cmd
        assert "--add-dir" in cmd
        assert str(tmp_path / ".state") in cmd
        assert cmd[-1] == "Do the work"

    def test_container_implement_command_uses_restricted_external_boundary(self, tmp_path):
        cmd = ClaudeAgent().build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
            externally_sandboxed=True,
        )

        assert "--dangerously-skip-permissions" not in cmd
        assert "--restricted" in cmd
        assert "--safe-mode" not in cmd
        assert cmd[cmd.index("--settings") + 1] == str(
            tmp_path / ".claude" / "settings.local.json"
        )

    def test_build_implement_command_with_stream_json(self, tmp_path):
        agent = ClaudeAgent()
        cmd = agent.build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
            stream_json=True,
        )
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_build_implement_command_with_mcp(self, tmp_path):
        mcp_path = tmp_path / "mcp.json"
        agent = ClaudeAgent()
        cmd = agent.build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
            mcp_config_path=mcp_path,
        )
        assert "--mcp-config" in cmd
        assert str(mcp_path) in cmd
        assert "--strict-mcp-config" in cmd

    def test_build_authoring_command(self, tmp_path):
        agent = ClaudeAgent()
        cmd = agent.build_authoring_command(
            prompt="Author a spec",
            worktree_path=tmp_path,
            initial_prompt="Start here",
            mcp_config_path=tmp_path / ".claude" / "mcp-servers.json",
        )
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert cmd[cmd.index("--settings") + 1] == str(
            tmp_path / ".claude" / "settings.local.json"
        )
        assert "--mcp-config" in cmd
        assert "--strict-mcp-config" not in cmd
        assert "--append-system-prompt" in cmd
        assert "Author a spec" in cmd
        assert "Start here" in cmd

    def test_build_review_command_is_restricted_read_only(self, tmp_path):
        agent = ClaudeAgent()
        cmd = agent.build_review_command(
            prompt="Review please",
            output_path=tmp_path / "review.json",
        )
        assert cmd[0:2] == ["claude", "-p"]
        assert "--restricted" in cmd
        assert "--safe-mode" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"
        assert "Review please" not in cmd
        assert cmd[-1] == "--no-session-persistence"

    def test_build_review_command_with_mcp_config(self, tmp_path):
        agent = ClaudeAgent()
        mcp_path = tmp_path / "mcp.json"
        cmd = agent.build_review_command(
            prompt="Review please",
            output_path=tmp_path / "review.json",
            mcp_config_path=mcp_path,
        )
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--restricted" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "--mcp-config" not in cmd
        assert str(mcp_path) not in cmd
        assert "--strict-mcp-config" in cmd
        assert "Review please" not in cmd
        assert cmd[-1] == "--no-session-persistence"

    def test_implements_protocol(self):
        assert isinstance(ClaudeAgent(), AgentAdapter)


class TestCodexAgent:
    def test_name(self):
        assert CodexAgent().name == "codex"

    def test_capabilities(self):
        caps = CodexAgent().capabilities
        assert caps.name == "codex"
        assert caps.supports_stream_json is False
        assert caps.supports_mcp is True
        assert caps.supports_network_access is True

    def test_build_implement_command(self, tmp_path):
        agent = CodexAgent()
        cmd = agent.build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
        )
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--json" in cmd
        assert "--add-dir" in cmd
        assert "Do the work" in cmd

    def test_build_implement_command_only_adds_private_linked_worktree_git_metadata(
        self,
        tmp_path,
    ):
        worktree = tmp_path / ".worktrees" / "feature"
        gitdir = tmp_path / ".git" / "worktrees" / "feature"
        common_git = tmp_path / ".git"
        private_git = gitdir / "specbutler-private-git"
        worktree.mkdir(parents=True)
        gitdir.mkdir(parents=True)
        private_git.mkdir()
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
        (common_git / "objects").mkdir()
        isolation = MagicMock()
        isolation.worktree = worktree.resolve()
        isolation.writable_paths = (private_git.resolve(),)
        isolation.read_only_paths = (
            (worktree / ".git").resolve(),
            gitdir.resolve(),
            (common_git / "objects").resolve(),
            (common_git / "refs").resolve(),
        )

        cmd = CodexAgent().build_implement_command(
            prompt="Do the work",
            worktree_path=worktree,
            state_dir=tmp_path / ".spec-state",
            git_isolation=isolation,
        )

        add_dirs = [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "--add-dir"]
        assert str(tmp_path / ".spec-state") in add_dirs
        assert str(private_git.resolve()) in add_dirs
        assert str(gitdir.resolve()) not in add_dirs
        assert str((common_git / "objects").resolve()) not in add_dirs
        assert str(common_git.resolve()) not in add_dirs

        config_overrides = [
            cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "-c"
        ]
        filesystem_policy = next(
            value
            for value in config_overrides
            if value.startswith("permissions.specbutler-implement.filesystem=")
        )
        assert f'{json.dumps(str(tmp_path / ".spec-state"))}="write"' in filesystem_policy
        assert f'{json.dumps(str(private_git.resolve()))}="write"' in filesystem_policy
        assert f'{json.dumps(str((worktree / ".git").resolve()))}="deny"' in filesystem_policy
        assert f'{json.dumps(str((common_git / "objects").resolve()))}="deny"' in filesystem_policy
        assert f'{json.dumps(str(gitdir.resolve()))}="write"' not in filesystem_policy
        assert f'{json.dumps(str(common_git.resolve()))}="write"' not in filesystem_policy

    def test_build_implement_command_refuses_unisolated_linked_worktree(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /tmp/untrusted\n")

        with pytest.raises(RuntimeError, match="without a prepared private Git"):
            CodexAgent().build_implement_command(
                prompt="Do the work",
                worktree_path=worktree,
                state_dir=tmp_path / ".spec-state",
            )

    def test_build_authoring_command_needs_no_external_path_for_full_clone(self, tmp_path):
        worktree = tmp_path / "checkout"
        gitdir = worktree / ".git"
        gitdir.mkdir(parents=True)

        cmd = CodexAgent().build_authoring_command(
            prompt="Author a spec",
            worktree_path=worktree,
            state_dir=tmp_path / ".spec-state",
            protected_env_keys={"AUTHORING_MCP_TOKEN"},
        )

        add_dirs = [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "--add-dir"]
        assert str(tmp_path / ".spec-state") in add_dirs
        assert str(gitdir.resolve()) not in add_dirs

        config_overrides = [
            cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "-c"
        ]
        filesystem_policy = next(
            value
            for value in config_overrides
            if value.startswith("permissions.specbutler-authoring.filesystem=")
        )
        assert f'{json.dumps(str(tmp_path / ".spec-state"))}="write"' in filesystem_policy
        assert f'{json.dumps(str(gitdir.resolve()))}="write"' not in filesystem_policy
        assert 'permissions.specbutler-authoring.network={enabled=false}' in cmd
        shell_policy = next(
            value
            for value in config_overrides
            if value.startswith("shell_environment_policy.exclude=")
        )
        assert '"AUTHORING_MCP_TOKEN"' in shell_policy

    def test_codex_git_metadata_dirs_ignores_non_git_directory(self, tmp_path):
        assert _codex_git_metadata_dirs(tmp_path) == []

    def test_build_implement_command_uses_scoped_permission_profile(self, tmp_path):
        agent = CodexAgent()
        cmd = agent.build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
        )
        assert "-s" not in cmd
        assert 'default_permissions="specbutler-implement"' in cmd
        assert any(
            value.startswith("permissions.specbutler-implement.filesystem=")
            for value in cmd
        )
        assert (
            'permissions.specbutler-implement.network='
            '{enabled=true,mode="full",allow_local_binding=true}'
        ) in cmd
        assert "--strict-config" in cmd
        assert "--ignore-rules" in cmd
        assert "features.shell_snapshot=false" in cmd
        for disabled_capability in (
            "features.browser_use=false",
            "features.browser_use_external=false",
            "features.browser_use_full_cdp_access=false",
            "features.computer_use=false",
            "features.image_generation=false",
            "features.in_app_browser=false",
            "features.plugins=false",
            "features.recommended_plugins=false",
            "features.skill_mcp_dependency_install=false",
            "features.skip_host_skill_discovery=true",
            "features.view_image=false",
        ):
            assert disabled_capability in cmd
        assert "features.use_legacy_landlock=true" not in cmd

    def test_build_implement_command_hides_mcp_bearer_env_from_shell(self, tmp_path):
        cmd = CodexAgent().build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
            mcp_servers={
                "remote": {
                    "url": "https://mcp.example.test",
                    "bearer_token_env_var": "MCP_BEARER_SECRET",
                },
                "stdio": {
                    "command": "helper",
                    "args": ["--token", "${MCP_CHILD_SECRET}"],
                },
            },
        )

        policy = next(
            value
            for value in cmd
            if value.startswith("shell_environment_policy.exclude=")
        )
        assert '"MCP_BEARER_SECRET"' in policy
        assert '"MCP_CHILD_SECRET"' in policy
        assert '"OPENAI_API_KEY"' in policy

    def test_build_review_command_uses_scratch_as_working_directory_not_writable_root(
        self, tmp_path
    ):
        agent = CodexAgent()
        scratch_dir = tmp_path / "review-scratch"
        cmd = agent.build_review_command(
            prompt="Review the work",
            output_path=tmp_path / "review.json",
            writable_temp_dir=scratch_dir,
        )
        assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "read-only"
        assert cmd[cmd.index("-C") + 1] == str(scratch_dir)
        assert "--skip-git-repo-check" in cmd
        assert "--add-dir" not in cmd
        assert cmd[-1] == "-"
        assert "Review the work" not in cmd
        assert "features.use_legacy_landlock=true" not in cmd

    def test_build_review_command_without_scratch_remains_read_only(self, tmp_path):
        cmd = CodexAgent().build_review_command(
            prompt="Review the work",
            output_path=tmp_path / "review.json",
        )

        assert cmd[cmd.index("-s") + 1] == "read-only"
        assert "-C" not in cmd

    def test_build_implement_command_keeps_default_sandbox(self, tmp_path):
        agent = CodexAgent()
        cmd = agent.build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
        )
        assert "features.use_legacy_landlock=true" not in cmd

    def test_build_authoring_command(self, tmp_path):
        agent = CodexAgent()
        cmd = agent.build_authoring_command(
            prompt="Author a spec",
            worktree_path=tmp_path,
            initial_prompt="Start here",
        )
        assert cmd[0] == "codex"
        # The combined prompt should contain both prompts
        assert any("Author a spec" in arg and "Start here" in arg for arg in cmd)
        # Interactive operator sessions may request approval,
        # unlike unattended implement runs which stay -a never.
        assert cmd[cmd.index("-a") + 1] == "on-request"
        implement_cmd = CodexAgent().build_implement_command(
            prompt="do it", worktree_path=tmp_path, state_dir=tmp_path / ".spec-state"
        )
        assert implement_cmd[implement_cmd.index("-a") + 1] == "never"

    def test_build_implement_command_does_not_set_env(self, tmp_path):
        """CodexAgent stays argv-only — env mutation is the orchestrator's job."""
        import os
        before = dict(os.environ)
        cmd = CodexAgent().build_implement_command(
            prompt="Do the work",
            worktree_path=tmp_path,
            state_dir=tmp_path / ".state",
        )
        assert cmd[0] == "codex"
        # CODEX_HOME is owned by the orchestrator, not the adapter.
        assert "CODEX_HOME" not in [a.split("=", 1)[0] for a in cmd]
        assert os.environ == before

    def test_implements_protocol(self):
        assert isinstance(CodexAgent(), AgentAdapter)


class TestRenderCodexMcpToml:
    """Tests for the file-write counterpart to _codex_mcp_server_overrides."""

    def test_empty_input_returns_empty_string(self):
        from spec_runtime.agent_adapter import _render_codex_mcp_toml

        assert _render_codex_mcp_toml({}) == ""
        assert _render_codex_mcp_toml(None) == ""

    def test_renders_command_server_with_args_and_env(self):
        from spec_runtime.agent_adapter import _render_codex_mcp_toml

        body = _render_codex_mcp_toml(
            {
                "playwright": {
                    "command": "/usr/local/bin/node",
                    "args": ["/path/to/cli.js", "--headless"],
                    "env": {"DEBUG": "1"},
                }
            }
        )
        assert "[mcp_servers.playwright]" in body
        assert 'command = "/usr/local/bin/node"' in body
        assert 'args = ["/path/to/cli.js", "--headless"]' in body
        assert 'default_tools_approval_mode = "approve"' in body
        assert "[mcp_servers.playwright.env]" in body
        assert 'DEBUG = "1"' in body

    def test_renders_url_server_with_bearer_token(self):
        from spec_runtime.agent_adapter import _render_codex_mcp_toml

        body = _render_codex_mcp_toml(
            {
                "remote": {
                    "url": "https://example.com/mcp",
                    "bearer_token_env_var": "REMOTE_TOKEN",
                }
            }
        )
        assert "experimental_use_rmcp_client = true" in body
        assert "[mcp_servers.remote]" in body
        assert 'url = "https://example.com/mcp"' in body
        assert 'bearer_token_env_var = "REMOTE_TOKEN"' in body

    def test_skips_server_with_invalid_name(self):
        from spec_runtime.agent_adapter import _render_codex_mcp_toml

        body = _render_codex_mcp_toml(
            {
                "bad name with spaces": {
                    "command": "/usr/bin/true",
                },
                "ok-name": {
                    "command": "/usr/bin/true",
                },
            }
        )
        assert "bad name with spaces" not in body
        assert "[mcp_servers.ok-name]" in body


class TestAgentAdapterFactory:
    def test_get_claude_adapter(self):
        _AGENT_REGISTRY.clear()
        adapter = get_agent_adapter("claude")
        assert adapter.name == "claude"

    def test_get_codex_adapter(self):
        _AGENT_REGISTRY.clear()
        adapter = get_agent_adapter("codex")
        assert adapter.name == "codex"

    def test_unknown_agent_raises(self):
        _AGENT_REGISTRY.clear()
        with pytest.raises(ValueError, match="Unknown agent"):
            get_agent_adapter("unknown-agent")

    def test_register_custom_adapter(self):
        _AGENT_REGISTRY.clear()
        mock = MagicMock(spec=AgentAdapter)
        mock.name = "custom"
        register_agent_adapter("custom", mock)
        assert get_agent_adapter("custom") is mock
        _AGENT_REGISTRY.clear()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLIMain:
    """Tests for spec_runtime.cli.main() dispatch."""

    def _import_cli(self):
        """Import (or reimport) the CLI module."""
        sys.modules.pop("spec_runtime.cli", None)
        from spec_runtime import cli

        return cli

    def test_windows_redirected_stdio_is_reconfigured_to_utf8(self):
        cli = self._import_cli()
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")

        cli._configure_windows_stdio(platform="win32", streams=(stream,))
        stream.write("snow-雪")
        stream.flush()

        assert stream.encoding.lower().replace("-", "") == "utf8"
        assert raw.getvalue() == "snow-雪".encode()

    def test_version_flag_prints_version_without_config(self, capsys):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config") as mock_config:
            with patch("importlib.metadata.version", return_value="1.2.3"):
                rc = cli.main(["--version"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "1.2.3"
        mock_config.assert_not_called()

    @pytest.mark.parametrize(
        "argv",
        (
            ["--help"],
            ["implement", "--help"],
            ["review", "--help"],
            ["auto", "run", "--help"],
        ),
    )
    def test_help_surfaces_do_not_load_repository_config(self, argv):
        cli = self._import_cli()
        with patch.object(
            cli,
            "_lazy_config",
            side_effect=SpecConfigNotFoundError(
                "Expected config file: C:\\venv\\Lib\\site-packages\\.spec.toml"
            ),
        ) as mock_config:
            with pytest.raises(SystemExit) as exc_info:
                cli.main(argv)

        assert exc_info.value.code == 0
        mock_config.assert_not_called()

    def test_root_help_lists_init_with_bootstrap_description(self, capsys):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config") as mock_config:
            with pytest.raises(SystemExit) as exc_info:
                cli.main(["--help"])

        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        assert "init" in output
        assert "Bootstrap a Git repository for spec-driven development" in output
        mock_config.assert_not_called()

    def test_init_help_uses_canonical_options_without_config(self, capsys):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config") as mock_config:
            with pytest.raises(SystemExit) as exc_info:
                cli.main(["init", "--help"])

        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        assert "--force" in output
        assert "--yolo" in output
        mock_config.assert_not_called()

    def test_source_id_flag_prints_identity_without_config(self, capsys):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config") as mock_config,
            patch(
                "spec_runtime.execution_backend.host_spec_runtime_source_id",
                return_value="1.2.3@abc123",
            ),
        ):
            rc = cli.main(["--source-id"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == "1.2.3@abc123"
        mock_config.assert_not_called()

    def test_update_command_dispatches_without_config(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config") as mock_config,
            patch.object(cli, "_cmd_update", return_value=0) as mock_update,
        ):
            rc = cli.main(["update"])
        assert rc == 0
        mock_update.assert_called_once()
        mock_config.assert_not_called()

    def test_init_runs_update_notice_only_after_successful_dispatch(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_maybe_print_update_notice_for_init") as mock_notice,
            patch.object(cli, "_cmd_init", return_value=0) as mock_init,
            patch.object(cli, "_lazy_config") as mock_config,
        ):
            rc = cli.main(["init"])
        assert rc == 0
        mock_notice.assert_called_once_with()
        mock_init.assert_called_once()
        mock_config.assert_not_called()

    def test_failed_init_has_no_update_notice_side_effect(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_maybe_print_update_notice_for_init") as mock_notice,
            patch.object(cli, "_cmd_init", return_value=1) as mock_init,
            patch.object(cli, "_lazy_config") as mock_config,
        ):
            rc = cli.main(["init"])

        assert rc == 1
        mock_notice.assert_not_called()
        mock_init.assert_called_once()
        mock_config.assert_not_called()

    def test_init_update_notice_falls_back_to_default_config_when_config_lookup_fails(self):
        cli = self._import_cli()

        with (
            patch("spec_runtime.config.load_spec_runtime_config", side_effect=RuntimeError("boom")),
            patch("spec_runtime.update.maybe_print_update_notice") as notice_mock,
            patch.object(cli, "_resolve_repo_root", return_value=Path("/tmp/repo")),
        ):
            cli._maybe_print_update_notice_for_init()

        notice_args = notice_mock.call_args.args
        assert notice_args[0] == Path("/tmp/repo")
        assert notice_args[1].paths.state_dir == ".spec-state"

    def test_non_init_commands_still_emit_update_notice_when_config_is_missing(self, capsys):
        cli = self._import_cli()

        with (
            patch.object(
                cli,
                "_lazy_config",
                side_effect=SpecConfigNotFoundError("missing .spec.toml"),
            ),
            patch("spec_runtime.update.maybe_print_update_notice") as notice_mock,
            patch.object(cli, "_resolve_repo_root", return_value=Path("/tmp/repo")),
        ):
            rc = cli.main(["list"])

        assert rc == 1
        assert "missing .spec.toml" in capsys.readouterr().err
        notice_args = notice_mock.call_args.args
        assert notice_args[0] == Path("/tmp/repo")
        assert notice_args[1].paths.state_dir == ".spec-state"

    def test_non_init_commands_still_emit_update_notice_before_other_config_failures(self):
        cli = self._import_cli()

        with (
            patch.object(cli, "_lazy_config", side_effect=RuntimeError("boom")),
            patch("spec_runtime.update.maybe_print_update_notice") as notice_mock,
            patch.object(cli, "_resolve_repo_root", return_value=Path("/tmp/repo")),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                cli.main(["list"])

        notice_args = notice_mock.call_args.args
        assert notice_args[0] == Path("/tmp/repo")
        assert notice_args[1].paths.state_dir == ".spec-state"

    def test_no_command_prints_help_and_returns_1(self):
        cli = self._import_cli()
        with patch.object(
            cli,
            "_lazy_config",
            return_value=MagicMock(
                agents=MagicMock(default="claude"),
                base_ref="master",
                retry_cap=5,
                paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
            ),
        ):
            rc = cli.main([])
        assert rc == 1

    def test_list_command_dispatches(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_cmd_list", return_value=0) as mock_list,
        ):
            rc = cli.main(["list"])
        assert rc == 0
        mock_list.assert_called_once()

    def test_show_command_dispatches(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_cmd_show", return_value=0) as mock_show,
        ):
            rc = cli.main(["show", "--spec", "my-spec"])
        assert rc == 0
        mock_show.assert_called_once()

    def test_clean_command_dispatches(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_cmd_clean", return_value=0) as mock_clean,
        ):
            rc = cli.main(["clean", "--spec", "old-spec"])
        assert rc == 0
        mock_clean.assert_called_once()

    def test_steer_command_dispatches_to_orchestrator(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        mock_orch = MagicMock()
        mock_orch.cmd_steer.return_value = 0
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_lazy_orchestrator", return_value=mock_orch),
        ):
            rc = cli.main(["steer", "--spec", "my-spec", "--message", "Prefer the smallest retry first."])
        assert rc == 0
        mock_orch.cmd_steer.assert_called_once()

    def test_implement_command_dispatches_to_orchestrator(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude", review_default="codex"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        mock_orch = MagicMock()
        mock_orch.cmd_run.return_value = 0
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_lazy_orchestrator", return_value=mock_orch),
        ):
            rc = cli.main(["implement", "--spec", "my-spec"])
        assert rc == 0
        mock_orch.cmd_run.assert_called_once()
        forwarded_args = mock_orch.cmd_run.call_args.args[0]
        assert forwarded_args.review_agent == "codex"

    def test_implement_command_accepts_explicit_review_agent(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude", review_default="codex"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        mock_orch = MagicMock()
        mock_orch.cmd_run.return_value = 0
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_lazy_orchestrator", return_value=mock_orch),
        ):
            rc = cli.main(["implement", "--spec", "my-spec", "--review-agent", "claude"])
        assert rc == 0
        forwarded_args = mock_orch.cmd_run.call_args.args[0]
        assert forwarded_args.review_agent == "claude"

    def test_status_command_dispatches_to_orchestrator(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        mock_orch = MagicMock()
        mock_orch.cmd_status.return_value = 0
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_lazy_orchestrator", return_value=mock_orch),
        ):
            rc = cli.main(["status", "--spec", "my-spec"])
        assert rc == 0
        mock_orch.cmd_status.assert_called_once()

    def test_review_command_dispatches_with_pull_request_number(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_cmd_review", return_value=0) as mock_review,
        ):
            rc = cli.main(["review", "--pr", "42"])

        assert rc == 0
        forwarded_args = mock_review.call_args.args[0]
        assert forwarded_args.pr == 42

    def test_review_command_uses_review_feedback_cli(self):
        cli = self._import_cli()
        args = argparse.Namespace(pr=42)
        with patch("spec_runtime.review_feedback.main", return_value=0) as review_main:
            rc = cli._cmd_review(args)

        assert rc == 0
        review_main.assert_called_once_with(["--pr", "42"])

    def test_report_command_dispatches_to_orchestrator(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        mock_orch = MagicMock()
        mock_orch.cmd_report.return_value = 0
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_lazy_orchestrator", return_value=mock_orch),
        ):
            rc = cli.main(["report", "--status", "ok"])
        assert rc == 0
        mock_orch.cmd_report.assert_called_once()

    def test_phase_command_dispatches_to_orchestrator(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        mock_orch = MagicMock()
        mock_orch.cmd_step.return_value = 0
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_lazy_orchestrator", return_value=mock_orch),
        ):
            rc = cli.main(["phase", "--spec", "my-spec", "--phase", "implement"])
        assert rc == 0
        mock_orch.cmd_step.assert_called_once()

    def test_input_command_preserves_missing_agent_override(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_cmd_input", return_value=0) as mock_input,
        ):
            rc = cli.main(["input", "--spec", "my-spec"])

        assert rc == 0
        forwarded_args = mock_input.call_args.args[0]
        assert forwarded_args.spec == "my-spec"
        assert forwarded_args.agent is None

    def test_input_command_accepts_explicit_agent_override(self):
        cli = self._import_cli()
        mock_config = MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )
        with (
            patch.object(cli, "_lazy_config", return_value=mock_config),
            patch.object(cli, "_cmd_input", return_value=0) as mock_input,
        ):
            rc = cli.main(["input", "--spec", "my-spec", "--agent", "codex"])

        assert rc == 0
        forwarded_args = mock_input.call_args.args[0]
        assert forwarded_args.spec == "my-spec"
        assert forwarded_args.agent == "codex"


class TestCLIShowCommand:
    """Test _cmd_show with a fake spec file on disk."""

    def test_show_prints_spec_content(self, tmp_path, capsys):
        from spec_runtime.cli import _cmd_show

        # Set up a fake spec directory
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()
        spec_file = spec_dir / "my-spec.md"
        spec_file.write_text("# My Spec\nSome content\n")

        mock_config = MagicMock()
        mock_config.paths.specs_dir = "specs"
        mock_config.paths.task_specs_dir = "specs/tasks"

        args = MagicMock()
        args.spec = "my-spec"

        with (
            patch("spec_runtime.cli._resolve_repo_root", return_value=tmp_path),
            patch("spec_runtime.cli._lazy_config", return_value=mock_config),
        ):
            rc = _cmd_show(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "# My Spec" in captured.out
        assert "Some content" in captured.out

    def test_show_returns_1_for_missing_spec(self, tmp_path, capsys):
        from spec_runtime.cli import _cmd_show

        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir()

        mock_config = MagicMock()
        mock_config.paths.specs_dir = "specs"
        mock_config.paths.task_specs_dir = "specs/tasks"

        args = MagicMock()
        args.spec = "nonexistent"

        with (
            patch("spec_runtime.cli._resolve_repo_root", return_value=tmp_path),
            patch("spec_runtime.cli._lazy_config", return_value=mock_config),
        ):
            rc = _cmd_show(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()


class TestResolveRepoRoot:
    def test_normalizes_linked_worktree_to_common_root(self, tmp_path):
        from spec_runtime import cli

        repo = tmp_path / "repo"
        worktree = repo / ".worktrees" / "code-my-feature"

        result = subprocess.CompletedProcess(
            ["git", "rev-parse", "--show-toplevel"],
            0,
            stdout=str(worktree) + "\n",
            stderr="",
        )

        with (
            patch("spec_runtime.git_common.subprocess.run", return_value=result),
            patch("spec_runtime.git_common.resolve_common_root", return_value=repo) as mock_common_root,
        ):
            assert cli._resolve_repo_root() == repo

        mock_common_root.assert_called_once_with(worktree)


class TestPhasePublishCwdPropagation:
    """Verify that phase_publish passes cwd=worktree_path to forge.find_pr_for_branch."""

    def test_publish_passes_worktree_cwd_to_find_pr(self, tmp_path):
        """Regression test for F1: find_pr_for_branch must receive cwd."""
        from spec_runtime import orchestrator as orch

        # Build a minimal RunState
        run = MagicMock()
        run.branch = "code/test-spec--abc"
        run.spec_id = "test-spec"
        run.run_id = "test-spec-abc"
        run.run_mode = "spec"
        run.publish_as_draft = False
        run.attempts = 1
        run.retry_cap = 5
        run.review_expected_head_sha = ""
        run.review_decision_status = ""
        run.last_error = ""

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # Create a minimal spec file
        spec_dir = worktree / "specs"
        spec_dir.mkdir()
        spec_file = spec_dir / "test-spec.md"
        spec_file.write_text("# Test\n- [ ] item\n")

        # Track what cwd is passed to find_pr_for_branch
        find_pr_cwd = {}
        mock_forge = MagicMock()
        mock_forge.find_pr_for_branch.return_value = PullRequest(number=10, url="http://pr/10")
        mock_forge.update_pr.return_value = True
        mock_forge.push_branch.return_value = PushResult(ok=True)

        def capture_find_pr(branch, **kwargs):
            find_pr_cwd["cwd"] = kwargs.get("cwd")
            return PullRequest(number=10, url="http://pr/10", is_draft=True)

        mock_forge.find_pr_for_branch.side_effect = capture_find_pr

        with (
            patch.object(orch, "resolve_worktree_path", return_value=worktree),
            patch.object(orch, "_worktree_branch_alignment_error", return_value=""),
            patch.object(orch, "_active_spec_path", return_value=spec_file),
            patch.object(orch, "_spec_path_in_tree", return_value=spec_file),
            patch.object(orch, "_ensure_run_spec_committed", return_value=True),
            patch.object(orch, "_extract_acceptance_checklist", return_value="- [ ] check"),
            patch.object(orch, "_forge", return_value=mock_forge),
            patch.object(orch, "_check_forge_auth", return_value=""),
            patch.object(orch, "_known_issues_markdown", return_value="None"),
            patch.object(orch, "_spec_path_for_run", return_value="specs/test-spec.md"),
            patch.object(orch, "format_pr_review_owner", return_value="@reviewer"),
            patch.object(orch, "_head_sha", return_value="abc123"),
            patch.object(orch, "_gate_status_path", return_value=tmp_path / "gate.json"),
            patch.object(orch, "_assert_publication_transition_safe"),
        ):
            orch.phase_publish(run, tmp_path)

        # The critical assertion: cwd must be the worktree path
        assert find_pr_cwd.get("cwd") == worktree, (
            f"find_pr_for_branch must receive cwd=worktree_path, got {find_pr_cwd.get('cwd')}"
        )
        mock_forge.mark_pr_draft.assert_not_called()


# ---------------------------------------------------------------------------
# CLI dispatch tests for unified autopilot commands (watch, gc, auto)
# ---------------------------------------------------------------------------


class TestCLIAutopilotCommands:
    """Tests that the unified autopilot commands dispatch correctly."""

    def _mock_config(self):
        return MagicMock(
            agents=MagicMock(default="claude"),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )

    def _import_cli(self):
        sys.modules.pop("spec_runtime.cli", None)
        from spec_runtime import cli

        return cli

    def test_watch_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["watch", "--help"])

    def test_gc_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["gc", "--help"])

    def test_auto_run_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["auto", "run", "--help"])

    def test_auto_stop_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["auto", "stop", "--help"])

    def test_auto_no_subcommand_exits_nonzero(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            rc = cli.main(["auto"])
        assert rc != 0

    def test_watch_dispatches_to_watch_command(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch.object(cli, "_cmd_watch", return_value=0) as mock_watch,
        ):
            rc = cli.main(["watch"])
        assert rc == 0
        mock_watch.assert_called_once()

    def test_gc_dispatches_to_gc_command(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch.object(cli, "_cmd_gc", return_value=0) as mock_gc,
        ):
            rc = cli.main(["gc"])
        assert rc == 0
        mock_gc.assert_called_once()

    def test_auto_run_dispatches_to_run_loop(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.autopilot.run_loop", return_value=0) as mock_run,
            patch("spec_runtime.autopilot.parse_notify_backends", return_value=["macos"]),
        ):
            rc = cli.main(["auto", "run"])
        assert rc == 0
        mock_run.assert_called_once()

    def test_auto_stop_dispatches_to_stop_command(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.autopilot.stop_command", return_value=0) as mock_stop,
        ):
            rc = cli.main(["auto", "stop"])
        assert rc == 0
        mock_stop.assert_called_once()
