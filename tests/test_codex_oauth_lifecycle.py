from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runtime import execution_backend as eb
from spec_runtime import orchestrator as orch
from spec_runtime import provider_env as pe
from spec_runtime.agent_adapter import CodexAgent
from spec_runtime.provider_env import (
    CodexOAuthReconciliationRetryableError,
    create_ephemeral_codex_home,
)
from spec_runtime.web import bridge_codex


def _oauth_payload(marker: str, *, account_id: str = "account-1") -> str:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": marker,
            "tokens": {
                "access_token": f"access-{marker}",
                "refresh_token": f"refresh-{marker}",
                "id_token": f"id-{marker}",
                "account_id": account_id,
            },
        },
        sort_keys=True,
    )


def _provider_source(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    source_home = tmp_path / "operator-codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text(
        _oauth_payload("initial"),
        encoding="utf-8",
    )
    return source_home, {
        "CODEX_HOME": str(source_home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def test_ephemeral_copy_reconciles_rotated_oauth_atomically(tmp_path: Path) -> None:
    source_home, env = _provider_source(tmp_path)
    original_inode = (source_home / "auth.json").stat().st_ino

    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    (isolated_home / "auth.json").write_text(
        _oauth_payload("rotated"),
        encoding="utf-8",
    )
    context.cleanup()

    source_auth = source_home / "auth.json"
    assert json.loads(source_auth.read_text(encoding="utf-8"))["last_refresh"] == "rotated"
    assert source_auth.stat().st_ino != original_inode
    if hasattr(os, "getuid"):
        assert stat.S_IMODE(source_auth.stat().st_mode) == 0o600
    assert not isolated_home.exists()


def test_oauth_launch_journal_is_durable_before_staged_auth_copy(tmp_path: Path) -> None:
    source_home, env = _provider_source(tmp_path)
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    staged_home = tmp_path / "staged-home"
    staged_home.mkdir()
    staged_auth = staged_home / "auth.json"
    journal_root = pe.specbutler_user_state_root(env) / "oauth-recovery"
    real_atomic_write = pe._atomic_write_private_bytes
    observed_journal = False

    def observe_copy_order(path: Path, payload: bytes) -> None:
        nonlocal observed_journal
        if path == staged_auth:
            journals = list(journal_root.glob("*.json"))
            assert len(journals) == 1
            assert json.loads(journals[0].read_bytes())["version"] == 2
            observed_journal = True
        real_atomic_write(path, payload)

    with patch.object(pe, "_atomic_write_private_bytes", side_effect=observe_copy_order):
        session = pe.copy_codex_auth_for_launch(
            source_home / "auth.json",
            staged_auth,
            source=env,
        )

    assert session is not None
    assert observed_journal
    session.finish()


def test_oauth_launch_journal_parent_identity_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    source_home, env = _provider_source(tmp_path)
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    source_auth = source_home / "auth.json"
    initial = source_auth.read_bytes()
    staged_home = tmp_path / "staged-home"
    staged_home.mkdir()
    staged_auth = staged_home / "auth.json"
    session = pe.copy_codex_auth_for_launch(source_auth, staged_auth, source=env)
    assert session is not None
    rotated = _oauth_payload("tamper-recovery").encode()
    staged_auth.write_bytes(rotated)
    journal = session._launch_journal
    record = json.loads(journal.read_text(encoding="utf-8"))
    original_inode = record["staging_parent_inode"]
    record["staging_parent_inode"] = original_inode + 1
    journal.write_text(json.dumps(record), encoding="utf-8")
    session._close()

    replacement_home = tmp_path / "replacement-home"
    replacement_home.mkdir()
    with pytest.raises(RuntimeError, match="staging directory identity changed") as raised:
        pe.copy_codex_auth_for_launch(
            source_auth,
            replacement_home / "auth.json",
            source=env,
        )

    assert "access-tamper-recovery" not in str(raised.value)
    assert source_auth.read_bytes() == initial
    assert not (replacement_home / "auth.json").exists()
    record["staging_parent_inode"] = original_inode
    journal.write_text(json.dumps(record), encoding="utf-8")
    replacement = pe.copy_codex_auth_for_launch(
        source_auth,
        replacement_home / "auth.json",
        source=env,
    )
    assert replacement is not None
    assert source_auth.read_bytes() == rotated
    replacement.finish()


@pytest.mark.parametrize("failure_point", ["source-read", "replace", "parent-fsync"])
def test_transient_oauth_reconcile_failure_retains_copy_and_exact_retry_succeeds(
    tmp_path: Path,
    failure_point: str,
) -> None:
    source_home, env = _provider_source(tmp_path)
    source_auth = (source_home / "auth.json").resolve()
    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    rotated = _oauth_payload(f"rotated-{failure_point}").encode()
    (isolated_home / "auth.json").write_bytes(rotated)
    failed = False

    real_read = pe._read_private_regular_file
    real_replace = pe.os.replace
    real_fsync = pe.os.fsync

    def flaky_read(path: Path) -> bytes:
        nonlocal failed
        if failure_point == "source-read" and path.resolve() == source_auth and not failed:
            failed = True
            raise OSError("injected canonical read failure")
        return real_read(path)

    def flaky_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        nonlocal failed
        if failure_point == "replace" and Path(destination).resolve() == source_auth and not failed:
            failed = True
            raise OSError("injected replace failure")
        real_replace(source, destination)

    def flaky_fsync(descriptor: int) -> None:
        nonlocal failed
        real_fsync(descriptor)
        if (
            failure_point == "parent-fsync"
            and not failed
            and source_auth.read_bytes() == rotated
        ):
            failed = True
            raise OSError("injected parent fsync failure")

    with (
        patch.object(pe, "_read_private_regular_file", side_effect=flaky_read),
        patch.object(pe.os, "replace", side_effect=flaky_replace),
        patch.object(pe.os, "fsync", side_effect=flaky_fsync),
        pytest.raises(CodexOAuthReconciliationRetryableError, match="recovery copy"),
    ):
        context.cleanup()

    recovery_files = list((tmp_path / "state" / "specbutler" / "oauth-recovery").glob("*.json"))
    assert failed
    assert not isolated_home.exists()
    assert len(recovery_files) == 1
    recovery_record = json.loads(recovery_files[0].read_text(encoding="utf-8"))
    assert base64.b64decode(recovery_record["candidate_base64"]) == rotated
    if hasattr(os, "getuid"):
        assert stat.S_IMODE(recovery_files[0].stat().st_mode) == 0o600

    # The failed launch can release its random staging directory. A new
    # copy-backed launch discovers and replays the durable record before it
    # copies canonical auth, including after an interpreter restart.
    replacement, replacement_home = create_ephemeral_codex_home(env, copy_auth=True)
    assert (replacement_home / "auth.json").read_bytes() == rotated
    replacement.cleanup()

    assert json.loads(source_auth.read_text(encoding="utf-8"))["last_refresh"] == (
        f"rotated-{failure_point}"
    )
    assert not isolated_home.exists()
    assert not recovery_files[0].exists()


def test_published_oauth_with_stuck_journal_is_replayed_without_secret_errors(
    tmp_path: Path,
) -> None:
    source_home, env = _provider_source(tmp_path)
    source_auth = source_home / "auth.json"
    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    rotated = _oauth_payload("journal-cleanup-retry").encode()
    (isolated_home / "auth.json").write_bytes(rotated)
    auth_session = context._auth_session
    assert auth_session is not None
    launch_journal = auth_session._launch_journal
    real_unlink = Path.unlink

    def fail_launch_journal_unlink(path: Path, *args, **kwargs) -> None:
        if path == launch_journal:
            raise OSError("injected launch-journal unlink failure")
        real_unlink(path, *args, **kwargs)

    with (
        patch.object(Path, "unlink", new=fail_launch_journal_unlink),
        pytest.raises(CodexOAuthReconciliationRetryableError) as raised,
    ):
        context.cleanup()

    message = str(raised.value)
    assert "next copy-backed Codex launch" in message
    assert "access-journal-cleanup-retry" not in message
    assert "refresh-journal-cleanup-retry" not in message
    assert source_auth.read_bytes() == rotated
    assert launch_journal.is_file()
    journal_payload = launch_journal.read_bytes()
    assert json.loads(journal_payload)["reconciled_sha256"] == sha256(rotated).hexdigest()
    assert rotated not in journal_payload

    replacement, replacement_home = create_ephemeral_codex_home(env, copy_auth=True)
    assert (replacement_home / "auth.json").read_bytes() == rotated
    replacement.cleanup()
    assert not list(
        (pe.specbutler_user_state_root(env) / "oauth-recovery").glob("*.json")
    )


def test_dual_inline_and_launch_recovery_replays_when_launch_mark_and_unlink_fail(
    tmp_path: Path,
) -> None:
    source_home, env = _provider_source(tmp_path)
    source_auth = source_home / "auth.json"
    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    rotated = _oauth_payload("dual-record-recovery").encode()
    (isolated_home / "auth.json").write_bytes(rotated)
    auth_session = context._auth_session
    assert auth_session is not None
    launch_journal = auth_session._launch_journal
    real_atomic_write = pe._atomic_write_private_bytes
    real_unlink = Path.unlink

    def fail_launch_journal_mark(path: Path, payload: bytes) -> None:
        if path == launch_journal:
            raise OSError("injected launch-journal mark failure")
        real_atomic_write(path, payload)

    def fail_launch_journal_unlink(path: Path, *args, **kwargs) -> None:
        if path == launch_journal:
            raise OSError("injected launch-journal unlink failure")
        real_unlink(path, *args, **kwargs)

    with (
        patch.object(
            pe,
            "_atomic_write_private_bytes",
            side_effect=fail_launch_journal_mark,
        ),
        patch.object(Path, "unlink", new=fail_launch_journal_unlink),
        pytest.raises(CodexOAuthReconciliationRetryableError),
    ):
        context.cleanup()

    recovery_root = pe.specbutler_user_state_root(env) / "oauth-recovery"
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in recovery_root.glob("*.json")
    ]
    assert sorted(record["version"] for record in records) == [1, 2]
    assert next(record for record in records if record["version"] == 2)[
        "reconciled_sha256"
    ] is None
    assert source_auth.read_bytes() == rotated
    assert not isolated_home.exists()

    replacement, replacement_home = create_ephemeral_codex_home(env, copy_auth=True)
    assert (replacement_home / "auth.json").read_bytes() == rotated
    replacement.cleanup()
    assert not list(recovery_root.glob("*.json"))


def test_oauth_recovery_is_discovered_after_process_restart(tmp_path: Path) -> None:
    source_home, env = _provider_source(tmp_path)
    source_auth = (source_home / "auth.json").resolve()
    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    rotated = _oauth_payload("restart-recovery").encode()
    (isolated_home / "auth.json").write_bytes(rotated)
    real_read = pe._read_private_regular_file
    failed = False

    def fail_canonical_once(path: Path) -> bytes:
        nonlocal failed
        if path.resolve() == source_auth and not failed:
            failed = True
            raise OSError("injected canonical read failure")
        return real_read(path)

    with (
        patch.object(pe, "_read_private_regular_file", side_effect=fail_canonical_once),
        pytest.raises(CodexOAuthReconciliationRetryableError),
    ):
        context.cleanup()

    repo_root = Path(__file__).resolve().parents[1]
    child_env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(repo_root / "src"),
    }
    script = """
import json
import sys
from pathlib import Path
from spec_runtime.provider_env import create_ephemeral_codex_home

source_home = Path(sys.argv[1])
state_home = Path(sys.argv[2])
env = {"CODEX_HOME": str(source_home), "XDG_STATE_HOME": str(state_home)}
context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
payload = json.loads((isolated_home / "auth.json").read_text(encoding="utf-8"))
context.cleanup()
raise SystemExit(0 if payload["last_refresh"] == "restart-recovery" else 17)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(source_home),
            str(tmp_path / "state"),
        ],
        cwd=repo_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(source_auth.read_text(encoding="utf-8"))["last_refresh"] == (
        "restart-recovery"
    )
    assert not list(
        (tmp_path / "state" / "specbutler" / "oauth-recovery").glob("*.json")
    )


def test_oauth_launch_journal_recovers_true_precleanup_process_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_home, env = _provider_source(tmp_path)
    source_auth = source_home / "auth.json"
    initial = source_auth.read_bytes()
    rotated = _oauth_payload("os-exit-recovery").encode()
    fake_home = tmp_path / "operator-home"
    local_app_data = tmp_path / "local-app-data"
    fake_home.mkdir()
    env["LOCALAPPDATA"] = str(local_app_data)
    monkeypatch.setenv("HOME", str(fake_home))
    journal_root = pe.specbutler_user_state_root(env) / "oauth-recovery"
    handshake = tmp_path / "abandoned-home.txt"
    repo_root = Path(__file__).resolve().parents[1]
    child_env = dict(os.environ)
    child_env["HOME"] = str(fake_home)
    child_env["PYTHONPATH"] = str(repo_root / "src")
    script = """
import os
import sys
from pathlib import Path
from spec_runtime.provider_env import create_ephemeral_codex_home

source_home = Path(sys.argv[1])
state_home = Path(sys.argv[2])
local_app_data = Path(sys.argv[3])
handshake = Path(sys.argv[4])
rotated = sys.argv[5].encode()
env = {
    "CODEX_HOME": str(source_home),
    "XDG_STATE_HOME": str(state_home),
    "LOCALAPPDATA": str(local_app_data),
}
context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
(isolated_home / "auth.json").write_bytes(rotated)
(isolated_home / "provider-runtime.json").write_text("stale", encoding="utf-8")
handshake.write_text(str(isolated_home), encoding="utf-8")
os._exit(0)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(source_home),
            str(tmp_path / "state"),
            str(local_app_data),
            str(handshake),
            rotated.decode(),
        ],
        cwd=repo_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    abandoned_home = Path(handshake.read_text(encoding="utf-8"))
    journals = list(journal_root.glob("*.json"))
    assert source_auth.read_bytes() == initial
    assert (abandoned_home / "auth.json").read_bytes() == rotated
    assert len(journals) == 1
    journal_payload = journals[0].read_bytes()
    journal = json.loads(journal_payload)
    assert journal["version"] == 2
    assert journal["kind"] == "copy-launch"
    assert "candidate_base64" not in journal
    for secret in (b"access-initial", b"refresh-initial", b"id-initial"):
        assert secret not in journal_payload

    replacement, replacement_home = create_ephemeral_codex_home(env, copy_auth=True)
    try:
        assert source_auth.read_bytes() == rotated
        assert (replacement_home / "auth.json").read_bytes() == rotated
        assert not abandoned_home.exists()
    finally:
        replacement.cleanup()

    assert not list(journal_root.glob("*.json"))


@pytest.mark.parametrize("auth_mode", ["api-key", "posix-symlink"])
def test_ephemeral_home_lease_garbage_collects_non_oauth_process_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
) -> None:
    if auth_mode == "posix-symlink" and os.name == "nt":
        pytest.skip("POSIX symlink-mode provider homes are not used on native Windows")
    source_home, env = _provider_source(tmp_path)
    fake_home = tmp_path / "operator-home"
    local_app_data = tmp_path / "local-app-data"
    fake_home.mkdir()
    env["LOCALAPPDATA"] = str(local_app_data)
    if auth_mode == "api-key":
        env["OPENAI_API_KEY"] = "api-key-crash-secret"
    monkeypatch.setenv("HOME", str(fake_home))
    handshake = tmp_path / f"abandoned-{auth_mode}-home.txt"
    repo_root = Path(__file__).resolve().parents[1]
    child_env = dict(os.environ)
    child_env["HOME"] = str(fake_home)
    child_env["PYTHONPATH"] = str(repo_root / "src")
    script = """
import os
import sys
from pathlib import Path
from spec_runtime.provider_env import create_ephemeral_codex_home

source_home = Path(sys.argv[1])
state_home = Path(sys.argv[2])
local_app_data = Path(sys.argv[3])
handshake = Path(sys.argv[4])
auth_mode = sys.argv[5]
env = {
    "CODEX_HOME": str(source_home),
    "XDG_STATE_HOME": str(state_home),
    "LOCALAPPDATA": str(local_app_data),
}
if auth_mode == "api-key":
    env["OPENAI_API_KEY"] = "api-key-crash-secret"
context, isolated_home = create_ephemeral_codex_home(
    env,
    copy_auth=auth_mode != "posix-symlink",
)
(isolated_home / "provider-runtime.json").write_text("stale", encoding="utf-8")
handshake.write_text(str(isolated_home), encoding="utf-8")
os._exit(0)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(source_home),
            str(tmp_path / "state"),
            str(local_app_data),
            str(handshake),
            auth_mode,
        ],
        cwd=repo_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    abandoned_home = Path(handshake.read_text(encoding="utf-8"))
    abandoned_auth = abandoned_home / "auth.json"
    assert abandoned_home.is_dir()
    if auth_mode == "api-key":
        assert b"api-key-crash-secret" in abandoned_auth.read_bytes()
    else:
        assert abandoned_auth.is_symlink()

    replacement, replacement_home = create_ephemeral_codex_home(
        env,
        copy_auth=auth_mode != "posix-symlink",
    )
    try:
        assert replacement_home != abandoned_home
        assert replacement_home.is_dir()
        assert not abandoned_home.exists()
    finally:
        replacement.cleanup()


def test_container_copy_journal_replays_without_removing_stable_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_home, _env = _provider_source(tmp_path)
    source_auth = source_home / "auth.json"
    rotated = _oauth_payload("container-os-exit").encode()
    provider_root = tmp_path / "run" / "provider-homes" / "codex"
    state_home = tmp_path / "state"
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    journal_root = pe.specbutler_user_state_root(os.environ) / "oauth-recovery"
    handshake = tmp_path / "abandoned-container-home.txt"
    repo_root = Path(__file__).resolve().parents[1]
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(repo_root / "src")
    script = """
import os
import sys
from pathlib import Path
from spec_runtime import orchestrator as orch

provider_root = Path(sys.argv[1])
source_home = Path(sys.argv[2])
handshake = Path(sys.argv[3])
rotated = sys.argv[4].encode()
home = orch._write_codex_isolated_home(
    provider_root,
    mcp_servers={},
    source_home=source_home,
    copy_auth=True,
)
(home / "auth.json").write_bytes(rotated)
(home / "provider-runtime.json").write_text("keep-until-owner-cleanup", encoding="utf-8")
handshake.write_text(str(home), encoding="utf-8")
os._exit(0)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(provider_root),
            str(source_home),
            str(handshake),
            rotated.decode(),
        ],
        cwd=repo_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    abandoned_home = Path(handshake.read_text(encoding="utf-8"))
    replacement_home = orch._write_codex_isolated_home(
        provider_root,
        mcp_servers={},
        source_home=source_home,
        copy_auth=True,
    )
    assert replacement_home == abandoned_home
    assert source_auth.read_bytes() == rotated
    assert (replacement_home / "auth.json").read_bytes() == rotated
    assert (replacement_home / "provider-runtime.json").is_file()

    orch._remove_codex_isolated_auth(provider_root, preserve_home=True)
    assert replacement_home.is_dir()
    assert list(replacement_home.iterdir()) == []
    assert not list(journal_root.glob("*.json"))


def test_copy_back_rejects_account_switch_and_keeps_canonical_auth(tmp_path: Path) -> None:
    source_home, env = _provider_source(tmp_path)
    original = (source_home / "auth.json").read_text(encoding="utf-8")
    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    (isolated_home / "auth.json").write_text(
        _oauth_payload("attacker", account_id="other-account"),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="different account"):
        context.cleanup()

    assert (source_home / "auth.json").read_text(encoding="utf-8") == original
    assert not isolated_home.exists()


def test_api_key_ephemeral_home_remains_one_way(tmp_path: Path) -> None:
    source_home = tmp_path / "operator-codex"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "original"}),
        encoding="utf-8",
    )
    env = {
        "CODEX_HOME": str(source_home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    context, isolated_home = create_ephemeral_codex_home(env, copy_auth=True)
    (isolated_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "changed"}),
        encoding="utf-8",
    )
    context.cleanup()

    assert json.loads(source_auth.read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "original"


def test_second_copy_backed_web_home_fails_promptly_and_recovers(tmp_path: Path) -> None:
    _source_home, env = _provider_source(tmp_path)
    first, _first_home = create_ephemeral_codex_home(env, copy_auth=True)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            create_ephemeral_codex_home(env, copy_auth=True)
        assert time.monotonic() - started < 1.0
    finally:
        first.cleanup()

    replacement, _replacement_home = create_ephemeral_codex_home(env, copy_auth=True)
    replacement.cleanup()


def test_explicit_api_key_bypasses_existing_oauth_copy_lock(tmp_path: Path) -> None:
    source_home, env = _provider_source(tmp_path)
    original_oauth = (source_home / "auth.json").read_text(encoding="utf-8")
    env["OPENAI_API_KEY"] = "explicit-api-key"

    first, first_home = create_ephemeral_codex_home(env, copy_auth=True)
    second, second_home = create_ephemeral_codex_home(env, copy_auth=True)
    try:
        for home in (first_home, second_home):
            assert json.loads((home / "auth.json").read_text(encoding="utf-8")) == {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "explicit-api-key",
            }
    finally:
        second.cleanup()
        first.cleanup()

    assert (source_home / "auth.json").read_text(encoding="utf-8") == original_oauth


def test_implement_policy_denies_the_whole_external_provider_home(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outbox = tmp_path / "outbox"
    provider_home = tmp_path / "private" / ".spec-codex-home"
    workspace.mkdir()
    outbox.mkdir()
    provider_home.mkdir(parents=True)

    command = CodexAgent().build_implement_command(
        prompt="implement",
        worktree_path=workspace,
        state_dir=outbox,
        provider_home=provider_home,
    )

    filesystem = next(
        value
        for value in command
        if value.startswith("permissions.specbutler-implement.filesystem=")
    )
    assert f'"{provider_home.resolve()}"="deny"' in filesystem
    assert f'"{provider_home.resolve() / "auth.json"}"' not in filesystem
    assert not (workspace / ".spec-codex-home").exists()


@pytest.mark.parametrize("failure", [RuntimeError("spawn failed"), asyncio.CancelledError()])
def test_web_prelaunch_error_or_cancel_reconciles_oauth(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    source_home, env = _provider_source(tmp_path)
    captured_home: list[Path] = []

    def isolated_home(_env: dict[str, str]):
        context, home = create_ephemeral_codex_home(env, copy_auth=True)
        captured_home.append(home)
        return context, home

    async def fail_spawn(_supervisor, _cmd, **kwargs):
        provider_home = Path(kwargs["env"]["CODEX_HOME"])
        (provider_home / "auth.json").write_text(
            _oauth_payload("web-terminal"),
            encoding="utf-8",
        )
        raise failure

    session = bridge_codex._CodexSession(cwd=str(tmp_path))
    with (
        patch.object(bridge_codex.shutil, "which", return_value="/usr/bin/codex"),
        patch.object(session, "_isolated_provider_home", side_effect=isolated_home),
        patch.object(bridge_codex.ProcessSupervisor, "spawn_async", new=fail_spawn),
        pytest.raises(type(failure), match="spawn failed" if isinstance(failure, RuntimeError) else None),
    ):
        asyncio.run(session.start("prompt"))

    assert json.loads((source_home / "auth.json").read_text())["last_refresh"] == "web-terminal"
    assert captured_home and not captured_home[0].exists()


@pytest.mark.parametrize("terminal", ["success", "error", "cancel"])
def test_orchestrator_launch_reconciles_container_oauth_on_every_terminal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    source_home, _env = _provider_source(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    provider_root = tmp_path / "run" / "provider-homes" / "codex"
    provider_root.mkdir(parents=True)
    home = orch._write_codex_isolated_home(
        provider_root,
        mcp_servers={},
        source_home=source_home,
        copy_auth=True,
    )
    run = SimpleNamespace(
        agent="codex",
        implement_launches=1,
        attempts=1,
        run_id="run-1",
        spec_id="feature",
    )
    plan = orch.ImplementLaunchPlan(
        use_stream_json=False,
        agent_env={"CODEX_HOME": str(home)},
        agent_cmd=["codex"],
        popen_kwargs={"cwd": tmp_path, "env": {}},
        codex_auth_root=provider_root,
        codex_preserve_home=True,
    )

    class Backend:
        def launch_agent(self, request, *, monitor):
            del request, monitor
            (home / "auth.json").write_text(
                _oauth_payload(f"container-{terminal}"),
                encoding="utf-8",
            )
            if terminal == "error":
                raise RuntimeError("launch failed")
            if terminal == "cancel":
                raise KeyboardInterrupt
            return eb.AgentResult(returncode=0)

    monkeypatch.setattr(orch, "_resolve_execution_backend", lambda: Backend())
    if terminal == "success":
        assert orch._launch_implement_attempt(run, tmp_path, tmp_path, plan) == 0
    elif terminal == "error":
        with pytest.raises(RuntimeError, match="launch failed"):
            orch._launch_implement_attempt(run, tmp_path, tmp_path, plan)
    else:
        with pytest.raises(KeyboardInterrupt):
            orch._launch_implement_attempt(run, tmp_path, tmp_path, plan)

    assert json.loads((source_home / "auth.json").read_text())["last_refresh"] == (
        f"container-{terminal}"
    )
    assert home.is_dir()
    assert list(home.iterdir()) == []
