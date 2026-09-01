from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runtime import cli
from spec_runtime import orchestrator as orch
from spec_runtime.web.chat_api import _cleanup_chat_worktree
from spec_runtime.worktree_safety import (
    UnsafeWorktreePathError,
    configured_worktrees_root,
    expected_run_worktree_names,
    paths_equal,
    validate_owned_worktree_path,
)


@pytest.mark.parametrize(
    "configured",
    (
        "../outside",
        ".worktrees/../../outside",
        "/var/tmp/spec-worktrees",
        r"C:\outside\spec-worktrees",
        r"..\outside",
        r"\\server\share\spec-worktrees",
    ),
)
def test_configured_worktree_root_rejects_portable_escape_syntax(
    tmp_path: Path,
    configured: str,
) -> None:
    with pytest.raises(UnsafeWorktreePathError):
        configured_worktrees_root(tmp_path / "repo", configured)


def test_configured_worktree_root_preserves_nested_repo_relative_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert configured_worktrees_root(repo, ".cache/spec/worktrees") == (
        repo / ".cache" / "spec" / "worktrees"
    )


def test_owned_worktree_target_rejects_parent_and_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".worktrees"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()

    with pytest.raises(UnsafeWorktreePathError):
        validate_owned_worktree_path(
            owner_root=root,
            target=".worktrees/../outside",
            relative_to=repo,
            expected_names=("code-feature--token",),
        )

    link = root / "code-feature--token"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(UnsafeWorktreePathError):
        validate_owned_worktree_path(
            owner_root=root,
            target=link,
            relative_to=repo,
            expected_names=(link.name,),
        )


def test_run_identity_authorizes_only_exact_generated_name() -> None:
    assert expected_run_worktree_names(
        "feature",
        "code/feature--20260901T120000",
    ) == ("code-feature--20260901T120000", "feature")
    assert expected_run_worktree_names(
        "feature",
        "code/other--20260901T120000",
    ) == ()


def test_windows_path_equality_normalizes_slashes_and_case() -> None:
    assert paths_equal(
        r"C:\Repo\.worktrees\code-feature--token",
        "c:/repo/.WORKTREES/CODE-FEATURE--TOKEN",
        windows=True,
    )
    assert not paths_equal(
        r"C:\Repo\.worktrees\code-feature--token",
        r"D:\Repo\.worktrees\code-feature--token",
        windows=True,
    )
    assert not paths_equal(
        r"C:\Repo\.worktrees\code-feature--token",
        r"C:\Repo\.worktrees-old\code-feature--token",
        windows=True,
    )


@pytest.mark.parametrize(
    "recorded_path",
    (
        "{outside}",
        ".worktrees/../outside",
        ".worktrees/code-other--token",
    ),
)
def test_phase_cleanup_rejects_tampered_persisted_run_path_without_writes(
    tmp_path: Path,
    recorded_path: str,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    rendered_path = recorded_path.format(outside=outside)
    if rendered_path.startswith(".worktrees/code-other"):
        (repo / rendered_path).mkdir(parents=True)
    run = orch.RunState(
        run_id="feature-20260901T120000",
        spec_id="feature",
        branch="code/feature--token",
        worktree_path=rendered_path,
    )
    backend = SimpleNamespace(identity=SimpleNamespace(backend="worktree"))

    with (
        patch.object(orch, "_resolve_execution_backend", return_value=backend),
        patch.object(
            orch,
            "run_subprocess",
            side_effect=AssertionError("unsafe cleanup must fail before Git"),
        ),
    ):
        result = orch.phase_cleanup(run, repo)

    assert result == "failed"
    assert "Refusing unsafe worktree cleanup" in run.last_error
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_phase_cleanup_rejects_worktree_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".worktrees"
    (repo / ".git").mkdir(parents=True)
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    link = root / "code-feature--token"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    run = orch.RunState(
        run_id="feature-20260901T120000",
        spec_id="feature",
        branch="code/feature--token",
        worktree_path=str(link),
    )
    backend = SimpleNamespace(identity=SimpleNamespace(backend="worktree"))

    with (
        patch.object(orch, "_resolve_execution_backend", return_value=backend),
        patch.object(
            orch,
            "run_subprocess",
            side_effect=AssertionError("unsafe cleanup must fail before Git"),
        ),
    ):
        result = orch.phase_cleanup(run, repo)

    assert result == "failed"
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_chat_cleanup_rejects_session_path_outside_owned_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".worktrees"
    root.mkdir(parents=True)
    outside = tmp_path / "outside" / "spec-session-token"
    outside.mkdir(parents=True)
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    with (
        patch.object(orch, "_worktrees_root", return_value=root),
        patch.object(
            orch,
            "run_subprocess",
            side_effect=AssertionError("unsafe chat cleanup must fail before Git"),
        ),
        pytest.raises(RuntimeError, match="unsafe chat worktree cleanup"),
    ):
        _cleanup_chat_worktree(
            repo,
            str(outside),
            "spec-authoring/token",
        )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(os.name != "nt", reason="native Windows path rendering probe")
def test_windows_git_registration_path_case_and_slashes_match(tmp_path: Path) -> None:
    target = tmp_path / ".worktrees" / "code-feature--token"
    git_spelling = str(target).replace("\\", "/").swapcase()
    listing = f"worktree {git_spelling}\nHEAD {'a' * 40}\n\n"
    completed = subprocess.CompletedProcess([], 0, listing, "")

    with patch.object(orch, "run_subprocess", return_value=completed):
        registered, error = orch._worktree_is_registered(tmp_path, target)

    assert error == ""
    assert registered is True


@pytest.mark.skipif(os.name != "nt", reason="native Windows path rendering probe")
def test_windows_cli_never_raw_deletes_registered_path_with_case_variant(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    root = repo / ".worktrees"
    target = root / "code-feature--token"
    root.mkdir(parents=True)
    git_spelling = str(target).replace("\\", "/").swapcase()
    listing = subprocess.CompletedProcess(
        [],
        0,
        f"worktree {git_spelling}\nHEAD {'a' * 40}\n\n",
        "",
    )
    removed = subprocess.CompletedProcess([], 0, "", "")

    with (
        patch.object(cli, "run_git", side_effect=(listing, removed)) as run_git,
        patch(
            "spec_runtime.platform_fs.remove_tree",
            side_effect=AssertionError("registered worktree must be removed through Git"),
        ),
    ):
        result = cli._remove_worktree_path(
            target,
            common_root=repo,
            worktrees_root=root,
            expected_names=(target.name,),
        )

    assert result == 1
    assert run_git.call_args_list[-1].args[0] == ["worktree", "remove", str(target), "--force"]
