from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.agent_adapter import (
    ClaudeAgent,
    CodexAgent,
    _codex_implement_permission_overrides,
    claude_restricted_mode_unavailability_reason,
    codex_isolation_unavailability_reason,
)
from spec_runtime.provider_env import (
    CLAUDE_PROVIDER_CREDENTIAL_ENV_KEYS,
    minimal_provider_environment,
    protected_operator_paths,
    provider_environment_overlay,
    sanitize_implement_setup_environment,
)


def test_direct_anthropic_environment_strips_unrelated_secrets() -> None:
    source = {
        "PATH": "/bin",
        "SSL_CERT_FILE": "/certs/ca.pem",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "GH_TOKEN": "github-secret",
        "DATABASE_URL": "postgres://secret",
        "AWS_ACCESS_KEY_ID": "ambient-aws",
        "AWS_SECRET_ACCESS_KEY": "ambient-aws-secret",
        "OPENAI_API_KEY": "other-provider-secret",
    }

    env = minimal_provider_environment("claude", source)

    assert env["PATH"] == "/bin"
    assert env["SSL_CERT_FILE"] == "/certs/ca.pem"
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert "GH_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "OPENAI_API_KEY" not in env


@pytest.mark.parametrize(
    ("mode", "credential", "value"),
    [
        ("CLAUDE_CODE_USE_BEDROCK", "AWS_SECRET_ACCESS_KEY", "aws-secret"),
        ("CLAUDE_CODE_USE_VERTEX", "GOOGLE_APPLICATION_CREDENTIALS", "/google/key.json"),
        ("CLAUDE_CODE_USE_FOUNDRY", "AZURE_CLIENT_SECRET", "azure-secret"),
    ],
)
def test_claude_cloud_credentials_only_survive_selected_mode(
    mode: str,
    credential: str,
    value: str,
) -> None:
    all_cloud_credentials = {
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/google/key.json",
        "AZURE_CLIENT_SECRET": "azure-secret",
    }
    source = {"PATH": "/bin", mode: "true", **all_cloud_credentials}

    env = minimal_provider_environment("claude", source)

    assert env[mode] == "true"
    assert env[credential] == value
    assert {
        key for key in all_cloud_credentials if key != credential
    }.isdisjoint(env)


def test_sdk_overlay_neutralizes_inherited_values() -> None:
    overlay = provider_environment_overlay(
        "claude",
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "provider-secret",
            "GH_TOKEN": "github-secret",
            "DATABASE_URL": "database-secret",
        },
    )

    assert overlay["ANTHROPIC_API_KEY"] == "provider-secret"
    assert overlay["GH_TOKEN"] == ""
    assert overlay["DATABASE_URL"] == ""


@pytest.mark.parametrize(
    ("provider", "provider_controls"),
    [
        (
            "claude",
            {
                "ANTHROPIC_BASE_URL": "https://attacker.invalid",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
        ),
        (
            "codex",
            {
                "OPENAI_BASE_URL": "https://attacker.invalid",
                "CODEX_HOME": "/attacker/profile",
            },
        ),
    ],
)
def test_setup_environment_cannot_reconfigure_or_instrument_provider(
    provider: str,
    provider_controls: dict[str, str],
) -> None:
    source = {
        **provider_controls,
        "PATH": "/attacker/bin",
        "PATHEXT": ".ATTACKER",
        "NODE_OPTIONS": "--require /checkout/steal.js",
        "LD_PRELOAD": "/checkout/steal.so",
        "DYLD_INSERT_LIBRARIES": "/checkout/steal.dylib",
        "PYTHONPATH": "/checkout/python",
        "SSL_CERT_FILE": "/checkout/attacker-ca.pem",
        "SPEC_COMPLETION_OUTBOX": "/attacker/outbox",
        "DATABASE_URL": "postgres://project-db",
        "DB_PASSWORD": "project-db-secret",
        "STRIPE_API_KEY": "project-stripe-secret",
    }

    admitted, blocked = sanitize_implement_setup_environment(provider, source)

    assert admitted == {
        "DATABASE_URL": "postgres://project-db",
        "DB_PASSWORD": "project-db-secret",
        "STRIPE_API_KEY": "project-stripe-secret",
    }
    assert set(blocked) == set(source) - set(admitted)


def test_provider_environment_preserves_cross_platform_profile_locations() -> None:
    source = {
        "PATH": r"C:\\Windows\\System32",
        "USERPROFILE": r"C:\\Users\\operator",
        "HOMEDRIVE": "C:",
        "HOMEPATH": r"\\Users\\operator",
        "APPDATA": r"C:\\Users\\operator\\AppData\\Roaming",
        "LOCALAPPDATA": r"C:\\Users\\operator\\AppData\\Local",
        "XDG_CONFIG_HOME": "/profiles/operator/config",
        "XDG_CACHE_HOME": "/profiles/operator/cache",
        "DATABASE_URL": "database-secret",
    }

    env = minimal_provider_environment("codex", source)

    for key in (
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    ):
        assert env[key] == source[key]
    assert "DATABASE_URL" not in env


def test_protected_operator_paths_cover_common_and_configured_credentials(
    tmp_path: Path,
) -> None:
    operator_home = tmp_path / "operator"
    source = {
        "CODEX_HOME": str(tmp_path / "custom-codex"),
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(
            tmp_path / "ecs-authorization-token"
        ),
        "GH_CONFIG_DIR": str(tmp_path / "custom-gh"),
        "HOME": str(tmp_path / "environment-home"),
        "KUBECONFIG": os.pathsep.join(
            (str(tmp_path / "kube-one"), str(tmp_path / "kube-two"))
        ),
        "SSH_AUTH_SOCK": str(tmp_path / "ssh-agent.sock"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    with patch("spec_runtime.provider_env.Path.home", return_value=operator_home):
        protected = set(protected_operator_paths(source))

    assert operator_home / ".ssh" in protected
    assert operator_home / ".aws" in protected
    assert operator_home / ".claude" in protected
    assert operator_home / ".codex" in protected
    assert operator_home / ".gitconfig" in protected
    assert operator_home / ".config" / "git" in protected
    assert tmp_path / "environment-home" / ".gitconfig" in protected
    assert tmp_path / "environment-home" / ".config" / "git" in protected
    assert tmp_path / "config" / "git" in protected
    assert tmp_path / "custom-codex" in protected
    assert tmp_path / "ecs-authorization-token" in protected
    assert tmp_path / "custom-gh" in protected
    assert tmp_path / "kube-one" in protected
    assert tmp_path / "kube-two" in protected
    assert tmp_path / "ssh-agent.sock" in protected
    assert tmp_path / "state" / "specbutler" in protected
    if sys.platform.startswith("linux"):
        assert Path("/proc") in protected


def test_claude_provider_credential_classification_covers_auth_transports() -> None:
    assert {
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_FEDERATED_TOKEN_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HTTPS_PROXY",
    }.issubset(CLAUDE_PROVIDER_CREDENTIAL_ENV_KEYS)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("codex") is None,
    reason="requires the installed Codex Linux sandbox",
)
def test_codex_implement_profile_enforces_write_and_secret_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "outbox"
    provider_home = tmp_path / "operator-codex"
    workspace.mkdir()
    state.mkdir()
    provider_home.mkdir()
    operator_home = tmp_path / "operator-home"
    (operator_home / ".config" / "git").mkdir(parents=True)
    global_gitconfig = operator_home / ".gitconfig"
    global_gitconfig.write_text("global-git-secret", encoding="utf-8")
    xdg_gitconfig = operator_home / ".config" / "git" / "config"
    xdg_gitconfig.write_text("xdg-git-secret", encoding="utf-8")
    # Production staging creates this deny target before launching Codex.  The
    # low-level sandbox canary invokes the permission profile directly, so it
    # must materialize the same target for bubblewrap's deny bind mount.
    isolated_home = workspace / ".spec-codex-home"
    isolated_home.mkdir()
    (isolated_home / "auth.json").write_text("isolated-auth", encoding="utf-8")
    (isolated_home / "config.toml").write_text("", encoding="utf-8")
    secret = provider_home / "auth.json"
    secret.write_text("sandbox-canary", encoding="utf-8")
    (workspace / "secret-link").symlink_to(secret)
    monkeypatch.setenv("CODEX_HOME", str(provider_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator_home))

    overrides = _codex_implement_permission_overrides(workspace, [state])
    config_args: list[str] = []
    for index, value in enumerate(overrides[:-1]):
        if value == "-c":
            config_args += ["-c", overrides[index + 1]]

    proc_canary = "specbutler-provider-env-canary"
    script = (
        "from pathlib import Path; "
        f"Path({str(workspace / 'created.txt')!r}).write_text('ok'); "
        f"Path({str(state / 'reported.txt')!r}).write_text('ok'); "
        f"targets={[str(secret), str(workspace / 'secret-link'), str(global_gitconfig), str(xdg_gitconfig)]!r}; "
        "results=[]; "
        "exec(\"for target in targets:\\n"
        " try:\\n  Path(target).read_bytes()\\n"
        " except OSError:\\n  results.append(target)\"); "
        f"canary={proc_canary.encode()!r}; leaks=[]; "
        "exec(\"for environ in Path('/proc').glob('[0-9]*/environ'):\\n"
        " try:\\n  payload=environ.read_bytes()\\n"
        " except OSError:\\n  continue\\n"
        " if canary in payload:\\n  leaks.append(str(environ))\"); "
        "print('unexpected-readable', [target for target in targets if target not in results]); "
        "print('proc-env-leaks', leaks); "
        "raise SystemExit(0 if len(results) == len(targets) and not leaks else 17)"
    )
    sandbox_env = minimal_provider_environment("codex")
    sandbox_env["OPENAI_API_KEY"] = proc_canary
    result = subprocess.run(
        [
            "codex",
            "sandbox",
            "-C",
            str(workspace),
            *config_args,
            "-P",
            "specbutler-implement",
            sys.executable,
            "-c",
            script,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=sandbox_env,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "ok"
    assert (state / "reported.txt").read_text(encoding="utf-8") == "ok"
    assert secret.read_text(encoding="utf-8") == "sandbox-canary"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("codex") is None,
    reason="requires the installed Codex Linux sandbox",
)
def test_codex_authoring_profile_allows_edits_but_blocks_network_and_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    provider_home = tmp_path / "operator-codex"
    workspace.mkdir()
    provider_home.mkdir()
    operator_home = tmp_path / "operator-home"
    (operator_home / ".config" / "git").mkdir(parents=True)
    global_gitconfig = operator_home / ".gitconfig"
    global_gitconfig.write_text("global-git-secret", encoding="utf-8")
    xdg_gitconfig = operator_home / ".config" / "git" / "config"
    xdg_gitconfig.write_text("xdg-git-secret", encoding="utf-8")
    secret = provider_home / "auth.json"
    secret.write_text("operator-secret", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(provider_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator_home))

    command = CodexAgent().build_authoring_command(
        prompt="probe",
        worktree_path=workspace,
        protected_env_keys={"USER_MCP_SECRET"},
    )
    config_args: list[str] = []
    for index, value in enumerate(command[:-1]):
        if value == "-c":
            config_args += ["-c", command[index + 1]]

    script = (
        "import os, socket\n"
        "from pathlib import Path\n"
        f"Path({str(workspace / 'authored.txt')!r}).write_text('ok')\n"
        f"secrets = {[str(secret), str(global_gitconfig), str(xdg_gitconfig)]!r}\n"
        "file_blocked = True\n"
        "for secret_path in secrets:\n"
        "    try:\n"
        "        Path(secret_path).read_bytes()\n"
        "    except OSError:\n"
        "        continue\n"
        "    file_blocked = False\n"
        "try:\n"
        "    socket.socket()\n"
        "except PermissionError:\n"
        "    network_blocked = True\n"
        "else:\n"
        "    network_blocked = False\n"
        "env_blocked = 'USER_MCP_SECRET' not in os.environ\n"
        "raise SystemExit(0 if file_blocked and network_blocked and env_blocked else 17)\n"
    )
    sandbox_env = minimal_provider_environment("codex")
    sandbox_env["USER_MCP_SECRET"] = "must-not-enter-shell"
    result = subprocess.run(
        [
            "codex",
            "sandbox",
            "-C",
            str(workspace),
            *config_args,
            "-P",
            "specbutler-authoring",
            sys.executable,
            "-c",
            script,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=sandbox_env,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (workspace / "authored.txt").read_text(encoding="utf-8") == "ok"
    assert secret.read_text(encoding="utf-8") == "operator-secret"


def test_implement_provider_home_replaces_all_profile_locations(tmp_path: Path) -> None:
    isolated_home = tmp_path / "isolated-home"
    env = orch._subprocess_env_with_home(
        {
            "HOME": "/operator",
            "USERPROFILE": r"C:\\Users\\operator",
            "APPDATA": r"C:\\Users\\operator\\AppData\\Roaming",
            "LOCALAPPDATA": r"C:\\Users\\operator\\AppData\\Local",
            "XDG_CONFIG_HOME": "/operator/config",
            "XDG_CACHE_HOME": "/operator/cache",
        },
        isolated_home,
    )

    assert env["HOME"] == str(isolated_home)
    assert env["USERPROFILE"] == str(isolated_home)
    assert env["APPDATA"] == str(isolated_home / "AppData" / "Roaming")
    assert env["LOCALAPPDATA"] == str(isolated_home / "AppData" / "Local")
    assert env["XDG_CONFIG_HOME"] == str(isolated_home / ".config")
    assert env["XDG_CACHE_HOME"] == str(isolated_home / ".cache")


def test_claude_review_command_is_restricted_and_has_no_mcp(tmp_path: Path) -> None:
    cmd = ClaudeAgent().build_review_command(
        prompt="review",
        output_path=tmp_path / "result.json",
        schema_path=tmp_path / "schema.json",
        mcp_config_path=tmp_path / "untrusted-mcp.json",
        writable_temp_dir=tmp_path / "evidence",
    )

    assert "--restricted" in cmd
    assert "--safe-mode" in cmd
    assert "--no-session-persistence" in cmd
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert str(tmp_path / "untrusted-mcp.json") not in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path / "evidence")


def test_claude_local_review_removes_checkout_controlled_mcp_before_launch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    review_worktree = tmp_path / "review-worktree"
    repo.mkdir()
    schema_path = repo / ".github" / "schemas" / "codex-review.schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text("{}\n", encoding="utf-8")
    spec_path = review_worktree / "specs" / "feature.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        "# Feature\n\nAcceptance: preserve the unchanged sentinel criterion.\n",
        encoding="utf-8",
    )
    mcp_path = review_worktree / ".claude" / "mcp-servers.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "attacker": {
                        "command": "sh",
                        "args": ["-c", "echo ${MCP_SECRET}"],
                        "env": {"MCP_SECRET": "checkout-secret"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    run = orch.RunState(
        run_id="review-run",
        spec_id="feature",
        branch="code/feature--run",
        review_agent="claude",
    )
    raw_review = json.dumps(
        {
            "decision": "approved",
            "summary": "No findings.",
            "reviewed_head_sha": "a" * 40,
            "reviewed_base_sha": "b" * 40,
            "findings": [],
        }
    )

    @contextmanager
    def fake_review_worktree(*_args: object, **_kwargs: object):
        yield review_worktree

    def fake_review_exec(
        _repo_root: Path,
        cmd: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert not mcp_path.exists()
        assert str(mcp_path) not in cmd
        assert "--mcp-config" not in cmd
        assert "preserve the unchanged sentinel criterion" in str(
            kwargs["input_text"]
        )
        assert all("sentinel criterion" not in arg for arg in cmd)
        return subprocess.CompletedProcess(cmd, 0, raw_review, "")

    with (
        patch.object(
            orch,
            "_temporary_review_worktree",
            side_effect=fake_review_worktree,
        ),
        patch.object(orch, "_render_local_review_prompt", return_value="review"),
        patch.object(orch, "_bootstrap_review_worktree", return_value=""),
        patch.object(orch, "_claude_review_evidence_prompt_note", return_value=""),
        patch.object(orch, "require_host_agent_available"),
        patch.object(
            orch,
            "_run_local_review_subprocess",
            side_effect=fake_review_exec,
        ),
    ):
        result, _path = orch._run_local_review(
            run,
            repo,
            repo_name="owner/repo",
            pr_number=1,
            pr_body="",
            expected_head_sha="a" * 40,
            expected_base_sha="b" * 40,
        )

    assert result.status == "approved"


def test_claude_restricted_review_preflight_requires_current_cli() -> None:
    current_help = " ".join(
        (
            "--restricted",
            "--safe-mode",
            "--permission-mode",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources",
            "--settings",
        )
    )
    assert claude_restricted_mode_unavailability_reason(current_help) == ""
    partial = claude_restricted_mode_unavailability_reason("--restricted --safe-mode")
    assert "--strict-mcp-config" in partial
    reason = claude_restricted_mode_unavailability_reason("Usage: claude")
    assert "lacks isolation controls" in reason
    assert "npm install -g @anthropic-ai/claude-code" in reason


def test_codex_isolation_preflight_requires_every_launch_control() -> None:
    exec_help = " ".join(
        (
            "--add-dir",
            "--ephemeral",
            "--ignore-rules",
            "--ignore-user-config",
            "--json",
            "--output-schema",
            "--strict-config",
        )
    )
    sandbox_help = "--permission-profile"

    assert codex_isolation_unavailability_reason(exec_help, sandbox_help) == ""
    reason = codex_isolation_unavailability_reason(
        exec_help.replace("--ignore-rules", ""),
        sandbox_help,
    )
    assert "--ignore-rules" in reason
    assert "npm install -g @openai/codex" in reason


def test_local_review_transports_large_prompt_over_stdin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cwd = tmp_path / "scratch"
    repo.mkdir()
    cwd.mkdir()
    prompt = "x" * (140 * 1024)
    cmd = [
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); print(len(data))",
    ]

    completed = orch._run_local_review_subprocess(
        repo,
        cmd,
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=10,
        input_text=prompt,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == str(len(prompt))
    assert all(len(argument) < 32_000 for argument in cmd)


def test_scoped_outbox_round_trip_uses_granted_channel_until_launch_cleanup(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    run_id = "feature-run"
    outbox = orch._prepare_scoped_agent_completion_outbox(repo, run_id, 4)
    args = argparse.Namespace(spec="", run="", status="ok", summary="done")
    report_env = {
        "SPEC_COMPLETION_OUTBOX": str(outbox),
        "SPEC_ID": "feature",
        "SPEC_RUN_ID": run_id,
        "SPEC_ATTEMPT": "2",
        "SPEC_IMPLEMENT_LAUNCH": "4",
    }

    with patch.dict(os.environ, report_env, clear=True):
        assert orch.cmd_report(args) == 0

    result, local = orch._load_matching_implement_result(
        repo_root=repo,
        worktree_path=worktree,
        run_id=run_id,
        attempt=2,
        launch_number=4,
        spec_id="feature",
    )

    assert result is not None
    assert result.status == "passed"
    assert result.summary == "done"
    assert local is False
    assert outbox.is_file()
    assert (repo / ".spec-state" / "runs" / run_id / "implement-result.json").is_file()
    orch._cleanup_scoped_agent_completion_outbox(repo, run_id, 4)
    assert not outbox.parent.exists()


def test_implement_command_receives_only_scoped_state_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "code-feature"
    scoped_outbox = orch._scoped_agent_completion_outbox_path(repo, "feature-run", 1)
    worktree.mkdir(parents=True)

    cmd = orch._build_agent_command(
        "claude",
        worktree,
        spec_id="feature",
        agent_state_dir=scoped_outbox.parent,
    )

    assert str(scoped_outbox.parent) in cmd
    assert str(repo / ".spec-state") not in cmd


def test_claude_isolated_credentials_are_scrubbed_after_launch(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    source_config = tmp_path / "claude.json"
    source_credentials = tmp_path / "credentials.json"
    worktree.mkdir()
    source_config.write_text('{"account":"operator"}\n', encoding="utf-8")
    source_credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-secret",
                    "refreshToken": "refresh-secret",
                }
            }
        ),
        encoding="utf-8",
    )

    home = orch._write_claude_isolated_home(
        worktree,
        source_config=source_config,
        source_credentials=source_credentials,
    )
    assert (home / ".claude.json").is_file()
    copied = json.loads((home / ".claude" / ".credentials.json").read_text())
    assert copied["claudeAiOauth"]["accessToken"] == "access-secret"
    assert "refreshToken" not in copied["claudeAiOauth"]

    orch._remove_claude_isolated_auth(worktree)

    assert not home.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows reparse-point coverage is separate")
def test_claude_cleanup_unlinks_planted_home_without_following_it(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "operator-file"
    sentinel.write_text("untouched", encoding="utf-8")
    (worktree / ".spec-claude-home").symlink_to(
        external, target_is_directory=True
    )

    orch._remove_claude_isolated_auth(worktree)

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not (worktree / ".spec-claude-home").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows reparse-point coverage is separate")
def test_claude_cleanup_does_not_follow_nested_symlink(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    home = worktree / ".spec-claude-home"
    home.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "operator-file"
    sentinel.write_text("untouched", encoding="utf-8")
    (home / ".claude").symlink_to(external, target_is_directory=True)

    orch._remove_claude_isolated_auth(worktree)

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not home.exists()


def test_claude_cleanup_failure_is_fatal(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    home = worktree / ".spec-claude-home"
    home.mkdir(parents=True)

    with (
        patch.object(orch, "remove_tree", side_effect=OSError("sharing violation")),
        pytest.raises(RuntimeError, match="sharing violation"),
    ):
        orch._remove_claude_isolated_auth(worktree)

    assert home.exists()


def test_claude_launch_failure_scrubs_and_resyncs_isolated_credentials(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    home = worktree / ".spec-claude-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text('{"token":"secret"}', encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text(
        '{"accessToken":"secret"}',
        encoding="utf-8",
    )
    run = SimpleNamespace(
        agent="claude",
        implement_launches=1,
        attempts=1,
        run_id="run-1",
        spec_id="feature",
    )
    plan = orch.ImplementLaunchPlan(
        use_stream_json=False,
        agent_env={},
        agent_cmd=["claude"],
        popen_kwargs={"cwd": worktree, "env": {}},
    )

    class FailingBackend:
        def launch_agent(self, _request: object, *, monitor: object) -> object:
            raise RuntimeError("launch failed")

    backend = FailingBackend()
    with (
        patch.object(orch, "_resolve_execution_backend", return_value=backend),
        patch.object(orch, "_sync_orchestrator_paths_into_workspace") as sync,
        pytest.raises(RuntimeError, match="launch failed"),
    ):
        orch._launch_implement_attempt(run, tmp_path, worktree, plan)

    assert not home.exists()
    sync.assert_called_once_with(
        backend,
        worktree,
        (".spec-claude-home",),
        required=True,
    )


def test_web_claude_sdk_receives_secret_neutralizing_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spec_runtime.web import bridge_claude

    captured: dict[str, object] = {}

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.allowed_tools: list[str] = []

    class FakeClient:
        def __init__(self, *, options: object) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    fake_sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ClaudeSDKClient=FakeClient,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.setattr(bridge_claude, "_sdk_available", lambda: True)

    with patch.dict(
        os.environ,
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "provider-secret",
            "GH_TOKEN": "github-secret",
            "DATABASE_URL": "database-secret",
        },
        clear=True,
    ):
        async def exercise() -> None:
            bridge = bridge_claude.ClaudeBridge()
            session_id = await bridge.start_session(
                "system",
                agent="claude",
                cwd=str(tmp_path),
            )
            await bridge.stop_session(session_id)

        asyncio.run(exercise())

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["ANTHROPIC_API_KEY"] == "provider-secret"
    assert child_env["GH_TOKEN"] == ""
    assert child_env["DATABASE_URL"] == ""


def test_web_codex_spawn_replaces_ambient_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spec_runtime.web import bridge_codex

    captured: dict[str, object] = {}

    class EmptyReader:
        async def readline(self) -> bytes:
            return b""

    class FakeProcess:
        returncode = 0
        stderr = EmptyReader()

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

    class FakeSupervisor:
        async def spawn_async(self, cmd: list[str], **kwargs: object) -> FakeProcess:
            captured["cmd"] = cmd
            captured.update(kwargs)
            child_env = kwargs.get("env")
            assert isinstance(child_env, dict)
            auth_path = Path(str(child_env["CODEX_HOME"])) / "auth.json"
            captured["auth_payload"] = json.loads(auth_path.read_text(encoding="utf-8"))
            return FakeProcess()

    monkeypatch.setattr(bridge_codex.shutil, "which", lambda _name: "/bin/codex")
    monkeypatch.setattr(
        bridge_codex,
        "ProcessSupervisor",
        lambda _mode: FakeSupervisor(),
    )
    private_git = tmp_path / "private-git"
    real_git = tmp_path / "real-git"
    session = bridge_codex._CodexSession(
        str(tmp_path),
        git_isolation=SimpleNamespace(
            env_overrides={
                "GIT_DIR": str(private_git),
                "GIT_WORK_TREE": str(tmp_path),
            },
            writable_paths=(private_git,),
            read_only_paths=(real_git,),
        ),
    )
    session._send_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ({"result": {}}, []),
            ({"result": {"thread": {"id": "thread-1"}}}, []),
        ]
    )

    async def exercise() -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "/bin",
                "CODEX_HOME": str(tmp_path / "missing-codex-home"),
                "OPENAI_API_KEY": "provider-secret",
                "GH_TOKEN": "github-secret",
                "DATABASE_URL": "database-secret",
            },
            clear=True,
        ):
            await session.start("system")
            await session.stop()

    asyncio.run(exercise())

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "OPENAI_API_KEY" not in child_env
    assert "CODEX_API_KEY" not in child_env
    assert "GH_TOKEN" not in child_env
    assert "DATABASE_URL" not in child_env
    assert child_env["CODEX_APP_SERVER"] == "1"
    assert child_env["GIT_DIR"] == str(private_git)
    assert child_env["GIT_WORK_TREE"] == str(tmp_path)
    command = captured["cmd"]
    assert isinstance(command, list)
    filesystem = next(
        item
        for item in command
        if isinstance(item, str)
        and item.startswith("permissions.specbutler-web.filesystem=")
    )
    assert f'{json.dumps(str(private_git))}="write"' in filesystem
    assert f'{json.dumps(str(real_git))}="read"' in filesystem
    assert captured["auth_payload"] == {
        "auth_mode": "apikey",
        "OPENAI_API_KEY": "provider-secret",
    }
