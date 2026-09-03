from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spec_runtime.git_publish_guard import (
    UnsafeRepositoryGitConfigError,
    apply_host_owned_publication_guard,
    assert_repository_publication_baseline,
    capture_repository_publication_baseline,
    github_repo_slug_from_remote_url,
    host_publication_git_environment,
)
from spec_runtime.provider_env import sanitize_implement_setup_environment


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_launch_guard_allows_commit_and_fetch_but_blocks_ordinary_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = tmp_path / "authenticated-local-sink.git"
    source = tmp_path / "source"
    assert _git(tmp_path, "init", "--bare", str(sink)).returncode == 0
    assert _git(tmp_path, "init", "-b", "main", str(source)).returncode == 0
    assert _git(source, "config", "user.name", "Spec Butler test").returncode == 0
    assert _git(source, "config", "user.email", "specbutler@example.invalid").returncode == 0
    (source / "first.txt").write_text("first\n", encoding="utf-8")
    assert _git(source, "add", "first.txt").returncode == 0
    assert _git(source, "commit", "-m", "first").returncode == 0
    assert _git(source, "remote", "add", "origin", str(sink)).returncode == 0
    assert _git(source, "push", "origin", "main").returncode == 0
    before = _git(sink, "rev-parse", "refs/heads/main").stdout.strip()

    guarded_env = {"PATH": os.environ.get("PATH", os.defpath)}
    # Host Git environment must not redirect pre-launch inspection away from
    # the worktree that the child will actually use.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated.git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    apply_host_owned_publication_guard(guarded_env, source)
    assert _git(source, "fetch", "origin", env=guarded_env).returncode == 0
    (source / "second.txt").write_text("second\n", encoding="utf-8")
    assert _git(source, "add", "second.txt", env=guarded_env).returncode == 0
    assert _git(source, "commit", "-m", "second", env=guarded_env).returncode == 0

    pushed = _git(source, "push", "origin", "main", env=guarded_env)
    assert pushed.returncode != 0
    assert "specbutler-no-push" in (pushed.stdout + pushed.stderr)
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    assert _git(sink, "rev-parse", "refs/heads/main").stdout.strip() == before


def test_guard_replaces_inherited_inline_git_config_and_setup_cannot_override_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "attacker-helper",
    }
    apply_host_owned_publication_guard(env, repo)
    rendered = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert rendered["credential.helper"] == ""
    assert "attacker-helper" not in rendered.values()

    admitted, blocked = sanitize_implement_setup_environment(
        "codex",
        {
            "DATABASE_URL": "postgres://project",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_GLOBAL": "/tmp/attacker-config",
            "GIT_SSH_COMMAND": "attacker-ssh",
        },
    )
    assert admitted == {"DATABASE_URL": "postgres://project"}
    assert set(blocked) == {
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_SSH_COMMAND",
    }


@pytest.mark.parametrize(
    ("key", "value", "category"),
    [
        ("remote.origin.url", "https://operator:secret@example.invalid/repo", "URL"),
        ("remote.origin.pushurl", "https://token@example.invalid/repo", "URL"),
        ("url.https://key-secret@example.invalid/.insteadOf", "https://github.com/", "URL"),
        ("submodule.private.url", "https://token@example.invalid/private", "URL"),
        ("lfs.url", "https://operator:secret@example.invalid/assets", "URL"),
        ("remote.scp.url", "operator:secret@example.invalid:private/repo", "URL"),
        ("http.https://example.invalid/.extraHeader", "Authorization: Bearer secret", "header"),
        ("http.https://example.invalid/.extraHeader", "Proxy-Authorization: secret", "header"),
        ("credential.helper", "!credential-command --token secret", "helper"),
        ("include.path", "/operator/private/gitconfig", "include"),
        ("includeIf.gitdir:/repo.path", "/operator/private/gitconfig", "include"),
    ],
)
def test_guard_rejects_agent_visible_local_git_credentials_without_echoing_values(
    tmp_path: Path,
    key: str,
    value: str,
    category: str,
) -> None:
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "config", key, value).returncode == 0

    with pytest.raises(UnsafeRepositoryGitConfigError) as caught:
        apply_host_owned_publication_guard({}, repo)

    assert category.casefold() in str(caught.value).casefold()
    assert value not in str(caught.value)
    assert key not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_guard_allows_credential_free_local_git_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "remote", "add", "origin", "https://example.invalid/repo").returncode == 0
    assert _git(repo, "remote", "add", "ssh", "ssh://git@example.invalid/repo").returncode == 0
    assert _git(repo, "remote", "add", "scp", "git@example.invalid:project/repo").returncode == 0
    assert _git(repo, "config", "http.https://example.invalid/.extraHeader", "Accept: application/json").returncode == 0

    env: dict[str, str] = {}
    apply_host_owned_publication_guard(env, repo)

    assert env["GIT_CONFIG_COUNT"]


def test_guard_inspects_shared_config_from_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "linked"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "config", "user.name", "Spec Butler test").returncode == 0
    assert _git(repo, "config", "user.email", "specbutler@example.invalid").returncode == 0
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    assert _git(repo, "commit", "-m", "initial").returncode == 0
    assert _git(repo, "worktree", "add", "-b", "agent", str(worktree)).returncode == 0
    assert _git(
        repo,
        "config",
        "url.https://shared-secret@example.invalid/.insteadOf",
        "https://github.com/",
    ).returncode == 0

    with pytest.raises(UnsafeRepositoryGitConfigError) as caught:
        apply_host_owned_publication_guard({}, worktree)

    assert "shared-secret" not in str(caught.value)


def test_publication_baseline_detects_post_launch_local_config_poisoning(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "remote", "add", "origin", "https://example.invalid/repo").returncode == 0
    remote_url, fingerprint = capture_repository_publication_baseline(repo)
    assert _git(repo, "config", "core.sshCommand", "post-launch-helper").returncode == 0

    with pytest.raises(UnsafeRepositoryGitConfigError, match="changed") as caught:
        assert_repository_publication_baseline(
            repo,
            expected_remote_url=remote_url,
            expected_config_fingerprint=fingerprint,
        )

    assert "post-launch-helper" not in str(caught.value)


@pytest.mark.parametrize(
    "remote_url",
    [
        "--upload-pack=attacker",
        "ext::sh -c attacker",
        "helper::repository",
        "helper://repository/path",
        "ssh://-oProxyCommand=attacker/repository",
        "git@-oProxyCommand=attacker:repository",
        "https://example.invalid/repository\n--upload-pack=attacker",
        " https://example.invalid/repository",
    ],
)
def test_publication_baseline_rejects_remote_operands_with_command_semantics(
    tmp_path: Path,
    remote_url: str,
) -> None:
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "config", "remote.origin.url", remote_url).returncode == 0

    with pytest.raises(UnsafeRepositoryGitConfigError) as caught:
        capture_repository_publication_baseline(repo)

    message = str(caught.value)
    assert "safe for host Git publication" in message
    assert remote_url not in message


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/specbutler/specbutler.git",
        "ssh://git@github.com/specbutler/specbutler.git",
        "git@github.com:specbutler/specbutler.git",
        "../local-repository.git",
        "/tmp/local repository.git",
    ],
)
def test_publication_baseline_accepts_ordinary_remote_operands(
    tmp_path: Path,
    remote_url: str,
) -> None:
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "config", "remote.origin.url", remote_url).returncode == 0

    captured, fingerprint = capture_repository_publication_baseline(repo)

    assert captured == remote_url
    assert len(fingerprint) == 64


def test_publication_baseline_detects_linked_worktree_config_poisoning(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "linked"
    assert _git(tmp_path, "init", "-b", "main", str(repo)).returncode == 0
    assert _git(repo, "config", "user.name", "Spec Butler test").returncode == 0
    assert _git(repo, "config", "user.email", "specbutler@example.invalid").returncode == 0
    assert _git(repo, "config", "extensions.worktreeConfig", "true").returncode == 0
    assert _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git").returncode == 0
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    assert _git(repo, "commit", "-m", "initial").returncode == 0
    assert _git(repo, "worktree", "add", "-b", "agent", str(worktree)).returncode == 0

    remote_url, fingerprint = capture_repository_publication_baseline(worktree)
    assert _git(worktree, "config", "--worktree", "core.sshCommand", "hostile-ssh").returncode == 0

    with pytest.raises(UnsafeRepositoryGitConfigError, match="changed"):
        assert_repository_publication_baseline(
            worktree,
            expected_remote_url=remote_url,
            expected_config_fingerprint=fingerprint,
        )


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        ("https://github.com/specbutler/specbutler.git", "specbutler/specbutler"),
        ("git@github.com:specbutler/specbutler.git", "specbutler/specbutler"),
        ("ssh://git@github.com/specbutler/specbutler", "specbutler/specbutler"),
        ("https://token@github.com/specbutler/specbutler.git", ""),
        ("https://gitlab.com/specbutler/specbutler.git", ""),
        ("file:///tmp/specbutler.git", ""),
    ],
)
def test_github_repo_slug_is_derived_only_from_unambiguous_github_remote(
    remote_url: str,
    expected: str,
) -> None:
    assert github_repo_slug_from_remote_url(remote_url) == expected


def test_host_publication_environment_disables_hooks_and_git_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/attacker/repository")
    monkeypatch.setenv("GIT_SSH_COMMAND", "attacker-ssh")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/attacker/hooks")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-secret")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "authorization: secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("CODEX_HOME", "/tmp/agent-selected-codex-home")

    env = host_publication_git_environment()
    rendered = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }

    assert "GIT_DIR" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_HOME" not in env
    assert rendered["core.hooksPath"] == os.devnull
    assert rendered["credential.interactive"] == "false"
    assert rendered["extensions.worktreeConfig"] == "false"
