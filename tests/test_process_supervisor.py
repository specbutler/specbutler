from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

import spec_runtime.process_supervisor as process_supervisor
from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessIdentity,
    ProcessSupervisor,
    SupervisionToken,
    adopt,
    identity_matches,
    inspect_process,
    legacy_pid_record_is_live,
    promote_payload_identity,
    run,
    terminate,
    terminate_legacy_pid_record,
)


def test_token_round_trip_preserves_reopenable_identity() -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, identity, 7, "owner", "token", 9)
    assert SupervisionToken.from_dict(token.to_dict()) == token
    assert token.version == 2
    assert token.control_relpath.endswith("/control.json")
    assert token.control_nonce


def test_identity_matches_accepts_executable_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "python-real"
    executable.write_text("binary", encoding="utf-8")
    alias = tmp_path / "python-alias"
    if os.name == "nt":
        os.link(executable, alias)
    else:
        alias.symlink_to(executable)
    expected = ProcessIdentity(42, "created", str(alias))
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda _pid: ProcessIdentity(42, "created", str(executable)),
    )

    assert identity_matches(expected)


def test_identity_matches_still_rejects_different_executables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_executable = tmp_path / "python-a"
    live_executable = tmp_path / "python-b"
    expected_executable.write_text("a", encoding="utf-8")
    live_executable.write_text("b", encoding="utf-8")
    expected = ProcessIdentity(42, "created", str(expected_executable))
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda _pid: ProcessIdentity(42, "created", str(live_executable)),
    )

    assert not identity_matches(expected)


def test_posix_identity_prefers_darwin_kernel_executable_over_ps_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_supervisor.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            "42 Wed Sep 2 08:38:07 2026 (python3.12)\n",
            "",
        ),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_darwin_process_executable",
        lambda _pid: "/Library/Frameworks/Python.framework/Versions/3.12/Resources/"
        "Python.app/Contents/MacOS/Python",
    )

    identity = process_supervisor._posix_identity(42)

    assert identity is not None
    assert identity.executable.endswith("Python.app/Contents/MacOS/Python")
    assert identity.command == "(python3.12)"


def test_identity_matches_accepts_darwin_framework_python_exec_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    framework = tmp_path / "Library" / "Frameworks" / "Python.framework" / "Versions" / "3.12"
    stub = framework / "bin" / "python"
    app = framework / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    stub.parent.mkdir(parents=True)
    app.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    app.write_text("app", encoding="utf-8")
    venv_launcher = tmp_path / "venv" / "bin" / "python"
    venv_launcher.parent.mkdir(parents=True)
    # Model the venv launcher's resolved target without requiring Windows
    # Developer Mode or administrator symlink privileges.  The behavior under
    # test is identity comparison after Path.resolve(), not symlink creation.
    venv_launcher.write_text("launcher", encoding="utf-8")
    real_resolve = Path.resolve

    def resolve_framework_alias(path: Path, strict: bool = False) -> Path:
        if path == venv_launcher:
            return stub
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_framework_alias)
    expected = ProcessIdentity(42, "created", str(venv_launcher))
    monkeypatch.setattr(process_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda _pid: ProcessIdentity(42, "created", str(app)),
    )

    assert identity_matches(expected)


def test_identity_matches_rejects_darwin_framework_transition_across_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    framework = tmp_path / "Library" / "Frameworks" / "Python.framework" / "Versions"
    stub = framework / "3.12" / "bin" / "python"
    app = framework / "3.13" / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    stub.parent.mkdir(parents=True)
    app.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    app.write_text("app", encoding="utf-8")
    expected = ProcessIdentity(42, "created", str(stub))
    monkeypatch.setattr(process_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda _pid: ProcessIdentity(42, "created", str(app)),
    )

    assert not identity_matches(expected)


def test_identity_matches_rejects_darwin_framework_transition_across_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / "Python.framework" / "Versions" / "3.12"
    second = tmp_path / "second" / "Python.framework" / "Versions" / "3.12"
    stub = first / "bin" / "python"
    app = second / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    stub.parent.mkdir(parents=True)
    app.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    app.write_text("app", encoding="utf-8")
    expected = ProcessIdentity(42, "created", str(stub))
    monkeypatch.setattr(process_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda _pid: ProcessIdentity(42, "created", str(app)),
    )

    assert not identity_matches(expected)


@pytest.mark.skipif(os.name != "posix", reason="POSIX durable token membership")
def test_posix_token_membership_accepts_darwin_framework_exec_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    framework = tmp_path / "Python.framework" / "Versions" / "3.12"
    stub = framework / "bin" / "python3.12"
    app = framework / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    stub.parent.mkdir(parents=True)
    app.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    app.write_text("app", encoding="utf-8")
    recorded = ProcessIdentity(42, "created", str(stub), f"{stub} worker.py")
    live = ProcessIdentity(42, "created", str(app), f"{app} worker.py")
    token = SupervisionToken(
        LifetimeMode.ADOPTABLE,
        recorded,
        7,
        "owner",
        "darwin-framework-transition",
        pgid=42,
        payload_identity=recorded,
    )
    monkeypatch.setattr(process_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(process_supervisor, "inspect_process", lambda _pid: live)

    assert process_supervisor.supervision_token_contains_process(token, live)


@pytest.mark.skipif(os.name != "posix", reason="POSIX durable token membership")
def test_posix_token_membership_rejects_reverse_darwin_framework_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    framework = tmp_path / "Python.framework" / "Versions" / "3.12"
    stub = framework / "bin" / "python3.12"
    app = framework / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    stub.parent.mkdir(parents=True)
    app.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    app.write_text("app", encoding="utf-8")
    recorded = ProcessIdentity(42, "created", str(app), f"{app} worker.py")
    live = ProcessIdentity(42, "created", str(stub), f"{stub} worker.py")
    token = SupervisionToken(
        LifetimeMode.ADOPTABLE,
        recorded,
        7,
        "owner",
        "darwin-framework-reverse-transition",
        pgid=42,
        payload_identity=recorded,
    )
    monkeypatch.setattr(process_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(process_supervisor, "inspect_process", lambda _pid: live)

    assert not process_supervisor.supervision_token_contains_process(token, live)


@pytest.mark.skipif(os.name != "posix", reason="native POSIX launch invariant")
def test_posix_spawn_records_session_group_without_post_launch_getpgid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_supervisor.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(AssertionError("must not race getpgid")),
    )
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        assert managed.token.pgid == managed.pid
    finally:
        managed.kill()
        managed.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="native POSIX retained ownership")
def test_posix_managed_process_termination_uses_retained_launch_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)

    managed.terminate(grace_seconds=0)
    managed.wait(timeout=5)

    assert managed.returncode is not None


def test_windows_job_name_uses_cross_session_namespace() -> None:
    assert process_supervisor._windows_job_name("boundary") == r"Global\SpecButler-boundary"


@pytest.mark.skipif(os.name != "posix", reason="legacy V1 tokens are rejected on Windows")
def test_legacy_v1_posix_token_remains_deserializable() -> None:
    identity = ProcessIdentity(42, "created", "/usr/bin/python", "python child.py")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        identity,
        7,
        "owner",
        "legacy-token",
        pgid=42,
        version=1,
    )

    assert SupervisionToken.from_dict(token.to_dict()) == token


@pytest.mark.skipif(os.name != "posix", reason="pre-upgrade V2 token was minted on POSIX")
def test_legacy_v2_posix_local_job_token_remains_deserializable() -> None:
    identity = ProcessIdentity(42, "created", "/usr/bin/python", "python child.py")
    payload = SupervisionToken(
        LifetimeMode.DETACHED,
        identity,
        7,
        "owner",
        "legacy-posix-v2",
        payload_identity=identity,
    ).to_dict()
    payload["job_name"] = r"Local\SpecButler-legacy-posix-v2"

    restored = SupervisionToken.from_dict(payload)

    assert restored.version == 2
    assert restored.job_name == r"Local\SpecButler-legacy-posix-v2"


def test_v2_token_parser_rejects_local_job_name_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(
        LifetimeMode.DETACHED,
        identity,
        7,
        "owner",
        "windows-global-only",
        payload_identity=identity,
    ).to_dict()
    payload["job_name"] = r"Local\SpecButler-windows-global-only"
    monkeypatch.setattr(process_supervisor.sys, "platform", "win32")

    with pytest.raises(ValueError, match="noncanonical Job name"):
        SupervisionToken.from_dict(payload)


@pytest.mark.parametrize(
    "missing",
    ["supervision_id", "job_name", "keeper_identity", "payload_identity", "control_relpath", "control_nonce"],
)
def test_v2_token_parser_does_not_mint_missing_security_fields(missing: str) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(LifetimeMode.DETACHED, identity, 7, "owner", "strict-token").to_dict()
    payload.pop(missing)

    with pytest.raises(ValueError, match="V2 supervision token is missing"):
        SupervisionToken.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 3, "unsupported supervision token version"),
        ("job_name", r"Session\SpecButler-strict-token", "noncanonical Job name"),
        ("control_relpath", "controls/other/control.json", "noncanonical control path"),
    ],
)
def test_v2_token_parser_rejects_noncanonical_ownership_metadata(
    field: str, value: object, message: str
) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(LifetimeMode.DETACHED, identity, 7, "owner", "strict-token").to_dict()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        SupervisionToken.from_dict(payload)


@pytest.mark.parametrize("identity_field", ["keeper_identity", "payload_identity"])
def test_v2_token_parser_rejects_nonpositive_identity(identity_field: str) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(LifetimeMode.DETACHED, identity, 7, "owner", "strict-token").to_dict()
    payload[identity_field] = {**payload[identity_field], "pid": 0}

    with pytest.raises(ValueError, match="positive PIDs"):
        SupervisionToken.from_dict(payload)


def test_identity_rejects_stale_creation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ProcessIdentity(os.getpid(), "old", sys.executable)
    monkeypatch.setattr(
        "spec_runtime.process_supervisor.inspect_process",
        lambda pid: ProcessIdentity(pid, "new", sys.executable),
    )
    assert identity_matches(expected) is False


def _write_reconcilable_boundary(
    root: Path,
    supervision_id: str,
    *,
    nonce: str = "cleanup-nonce",
) -> tuple[SupervisionToken, Path, Path]:
    keeper = ProcessIdentity(4101, "keeper-start", "python.exe")
    payload = ProcessIdentity(4102, "payload-start", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        keeper.pid,
        keeper.started_at,
        supervision_id,
        payload_identity=payload,
        control_nonce=nonce,
    )
    control_path = root / token.control_relpath
    metadata_path = root / "metadata" / f"{supervision_id}.json"
    control_path.parent.mkdir(parents=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "supervision_id": supervision_id,
                "job_name": token.job_name,
                "nonce": nonce,
                "keeper_identity": keeper.to_dict(),
                "payload_identity": payload.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    metadata_path.write_text(json.dumps(token.to_dict()), encoding="utf-8")
    process_supervisor._durable_publication_ack_path(metadata_path).write_text(
        json.dumps(
            {
                "schema": 1,
                "supervision_id": supervision_id,
                "nonce": nonce,
                "keeper_identity": keeper.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    return token, control_path, metadata_path


def test_reconcile_stale_control_state_removes_authenticated_dead_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    token, control_path, metadata_path = _write_reconcilable_boundary(root, "dead-boundary")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", lambda _name: True)

    assert process_supervisor.reconcile_stale_control_state() == 1
    assert not control_path.exists()
    assert not control_path.with_suffix(".lock").exists()
    assert not metadata_path.exists()
    assert metadata_path.parent.is_dir()
    assert not process_supervisor._durable_publication_ack_path(metadata_path).exists()
    assert token.token == "dead-boundary"


def test_retire_inactive_control_state_removes_exact_stopped_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    token, control_path, metadata_path = _write_reconcilable_boundary(
        root,
        "just-stopped-boundary",
    )
    monkeypatch.setattr(process_supervisor, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        process_supervisor,
        "_windows_job_definitively_absent",
        lambda _name: True,
    )

    assert process_supervisor.retire_inactive_control_state(token)
    assert not control_path.exists()
    assert not control_path.with_suffix(".lock").exists()
    assert not metadata_path.exists()


def test_retire_inactive_control_state_preserves_nonce_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    token, control_path, metadata_path = _write_reconcilable_boundary(
        root,
        "mismatched-stopped-boundary",
    )
    mismatched = SupervisionToken(
        token.mode,
        token.identity,
        token.owner_pid,
        token.owner_started_at,
        token.token,
        payload_identity=token.payload,
        job_name=token.job_name,
        control_relpath=token.control_relpath,
        control_nonce="different-nonce",
    )
    monkeypatch.setattr(process_supervisor, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        process_supervisor,
        "_windows_job_definitively_absent",
        lambda _name: True,
    )

    assert not process_supervisor.retire_inactive_control_state(mismatched)
    assert control_path.exists()
    assert metadata_path.exists()


def test_retire_inactive_control_state_rejects_same_id_replacement_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    token, control_path, metadata_path = _write_reconcilable_boundary(
        root,
        "replaced-stopped-boundary",
    )
    monkeypatch.setattr(process_supervisor, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        process_supervisor,
        "_windows_job_definitively_absent",
        lambda _name: True,
    )
    reconcile = process_supervisor._reconcile_control_record

    def replace_before_locked_revalidation(
        path: Path,
        supervision_id: str,
        *,
        expected_token: SupervisionToken | None = None,
    ) -> bool:
        replacement = json.loads(path.read_text(encoding="utf-8"))
        replacement["nonce"] = "replacement-nonce"
        path.write_text(json.dumps(replacement), encoding="utf-8")
        return reconcile(
            path,
            supervision_id,
            expected_token=expected_token,
        )

    monkeypatch.setattr(
        process_supervisor,
        "_reconcile_control_record",
        replace_before_locked_revalidation,
    )

    assert not process_supervisor.retire_inactive_control_state(token)
    assert control_path.exists()
    assert metadata_path.exists()


def test_windows_job_absence_requires_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", lambda _name: None)
    monkeypatch.setattr(process_supervisor.ctypes, "get_last_error", lambda: 5, raising=False)
    assert not process_supervisor._windows_job_definitively_absent("Global\\denied")
    monkeypatch.setattr(process_supervisor.ctypes, "get_last_error", lambda: 2)
    assert process_supervisor._windows_job_definitively_absent("Global\\missing")


def test_reconcile_stale_control_state_preserves_a_live_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    token, control_path, metadata_path = _write_reconcilable_boundary(root, "live-boundary")
    monkeypatch.setattr(
        process_supervisor,
        "identity_matches",
        lambda identity: identity == token.identity,
    )
    absent = MagicMock(return_value=True)
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", absent)

    assert process_supervisor.reconcile_stale_control_state() == 0
    assert control_path.exists()
    assert metadata_path.exists()
    absent.assert_not_called()


def test_reconcile_stale_control_state_preserves_mismatched_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    token, control_path, metadata_path = _write_reconcilable_boundary(root, "split-boundary")
    mismatched = SupervisionToken(
        token.mode,
        token.identity,
        token.owner_pid,
        token.owner_started_at,
        token.token,
        payload_identity=token.payload,
        control_nonce="different-nonce",
    )
    metadata_path.write_text(json.dumps(mismatched.to_dict()), encoding="utf-8")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", lambda _name: True)

    assert process_supervisor.reconcile_stale_control_state() == 1
    assert not control_path.exists()
    assert metadata_path.exists()
    assert process_supervisor._durable_publication_ack_path(metadata_path).exists()


def test_reconcile_stale_control_state_preserves_unmarked_legacy_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    _token, control_path, metadata_path = _write_reconcilable_boundary(root, "legacy-boundary")
    state = json.loads(control_path.read_text(encoding="utf-8"))
    state.pop("job_name")
    control_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    absent = MagicMock(return_value=True)
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", absent)

    assert process_supervisor.reconcile_stale_control_state() == 0
    assert control_path.exists()
    assert metadata_path.exists()
    absent.assert_not_called()


def test_reconcile_stale_control_state_revalidates_after_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    _token, control_path, metadata_path = _write_reconcilable_boundary(root, "racing-boundary")
    checks = iter((True, False))
    monkeypatch.setattr(
        process_supervisor,
        "_control_boundary_is_inactive",
        lambda *_args: next(checks),
    )

    assert process_supervisor.reconcile_stale_control_state() == 0
    assert control_path.exists()
    assert metadata_path.exists()


def test_reconcile_stale_control_state_removes_aged_lock_only_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    lock_path = root / "controls" / "lock-only" / "control.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(b"\0")
    stale_time = time.time() - process_supervisor._ORPHAN_LOCK_MIN_AGE_SECONDS - 1
    os.utime(lock_path, (stale_time, stale_time))
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", lambda _name: True)

    assert process_supervisor.reconcile_stale_control_state() == 1
    assert not lock_path.exists()
    assert not lock_path.parent.exists()


def test_reconcile_stale_control_state_preserves_recent_or_live_lock_only_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    recent = root / "controls" / "recent" / "control.lock"
    live = root / "controls" / "live" / "control.lock"
    for lock_path in (recent, live):
        lock_path.parent.mkdir(parents=True)
        lock_path.write_bytes(b"\0")
    stale_time = time.time() - process_supervisor._ORPHAN_LOCK_MIN_AGE_SECONDS - 1
    os.utime(live, (stale_time, stale_time))
    monkeypatch.setattr(
        process_supervisor,
        "_windows_job_definitively_absent",
        lambda name: not name.endswith("-live"),
    )

    assert process_supervisor.reconcile_stale_control_state() == 0
    assert recent.exists()
    assert live.exists()


def test_reconcile_stale_control_state_removes_authenticated_orphan_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    _token, control_path, metadata_path = _write_reconcilable_boundary(root, "orphan-metadata")
    control_path.unlink()
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", lambda _name: True)

    assert process_supervisor.reconcile_stale_control_state() == 1
    assert not metadata_path.exists()
    assert not process_supervisor._durable_publication_ack_path(metadata_path).exists()
    assert not control_path.with_suffix(".lock").exists()


def test_reconciliation_never_removes_shared_metadata_directory(tmp_path: Path) -> None:
    control_path = tmp_path / "controls" / "finished" / "control.json"
    metadata_path = tmp_path / "metadata" / "finished.json"
    control_path.parent.mkdir(parents=True)
    metadata_path.parent.mkdir(parents=True)

    process_supervisor._remove_empty_control_artifacts(control_path, metadata_path)

    assert not control_path.parent.exists()
    # Publishers create atomic-write temporary files in this shared directory.
    # Its lifetime cannot depend on a different boundary finishing cleanup.
    assert metadata_path.parent.is_dir()


def test_reconcile_cursor_advances_past_full_retained_batch_for_later_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    controls_root = root / "controls"
    retained_count = process_supervisor._CONTROL_RECONCILE_LIMIT + 1
    for index in range(retained_count):
        retained = controls_root / f"a-retained-{index:03d}"
        retained.mkdir(parents=True)
        (retained / "control.json").write_text("{}", encoding="utf-8")

    _control_token, control_path, control_metadata = _write_reconcilable_boundary(
        root,
        "z-reclaimable-control",
    )
    _metadata_token, metadata_control, orphan_metadata = _write_reconcilable_boundary(
        root,
        "z-reclaimable-metadata",
    )
    metadata_control.unlink()
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        process_supervisor,
        "_windows_job_definitively_absent",
        lambda _name: True,
    )

    process_supervisor._ensure_control_state_reconciled()

    assert control_path.exists()
    assert control_metadata.exists()
    assert orphan_metadata.exists()
    cursor_path = root / process_supervisor._CONTROL_RECONCILE_CURSOR_FILENAME
    assert json.loads(cursor_path.read_text(encoding="utf-8"))["after"] == "a-retained-255/1-control"

    process_supervisor._ensure_control_state_reconciled()

    assert not control_path.exists()
    assert not control_metadata.exists()
    assert not orphan_metadata.exists()
    assert not metadata_control.parent.exists()
    assert all((controls_root / f"a-retained-{index:03d}").is_dir() for index in range(retained_count))


def test_current_process_retirement_requires_exact_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    identity = ProcessIdentity(os.getpid(), "current-start", sys.executable)
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        identity,
        identity.pid,
        identity.started_at,
        "current-boundary",
        payload_identity=identity,
        control_nonce="current-nonce",
    )
    control_path = root / token.control_relpath
    control_path.parent.mkdir(parents=True)
    state = {
        "schema": 2,
        "supervision_id": token.token,
        "job_name": token.job_name,
        "nonce": "newer-claim",
        "keeper_identity": identity.to_dict(),
        "payload_identity": identity.to_dict(),
    }
    control_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda candidate: candidate == identity)

    assert not process_supervisor._retire_current_process_control(token, control_path)
    assert control_path.exists()
    state["nonce"] = token.control_nonce
    control_path.write_text(json.dumps(state), encoding="utf-8")
    assert process_supervisor._retire_current_process_control(token, control_path)
    assert not control_path.exists()
    assert not control_path.with_suffix(".lock").exists()


def test_current_process_claim_quiesces_monitor_before_retirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        ProcessIdentity(os.getpid(), "current-start", sys.executable),
        os.getpid(),
        "current-start",
        "quiesce-current-monitor",
    )
    control_path = tmp_path / "control.json"
    monitor_stop = threading.Event()
    calls: list[object] = []

    class MonitorThread:
        def join(self, *, timeout: float) -> None:
            calls.append(("join", timeout, monitor_stop.is_set()))

    monkeypatch.setattr(
        process_supervisor,
        "_retire_current_process_control",
        lambda candidate, path: calls.append(("retire", candidate, path)) or True,
    )

    assert process_supervisor._retire_current_process_claim(
        token,
        control_path,
        monitor_stop,
        MonitorThread(),  # type: ignore[arg-type]
    )
    assert calls == [
        ("join", 1.0, True),
        ("retire", token, control_path),
    ]


def test_detached_durable_token_publication_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    supervision_id = f"published-{uuid.uuid4().hex}"
    ready = tmp_path / "payload-ready"
    payload = (
        "from pathlib import Path; import time; "
        f"Path({str(ready)!r}).write_text('ready', encoding='utf-8'); "
        "time.sleep(30)"
    )
    managed = ProcessSupervisor(
        LifetimeMode.DETACHED,
        supervision_id=supervision_id,
        publish_durable_token=True,
    ).spawn([sys.executable, "-c", payload])
    metadata_path = process_supervisor.durable_metadata_path(supervision_id)
    terminated_via_persisted_token = False
    try:
        deadline = time.monotonic() + 5
        while not ready.is_file() and managed.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.is_file(), "detached payload did not reach post-exec readiness"
        published = SupervisionToken.from_dict(
            json.loads(metadata_path.read_text(encoding="utf-8"))
        )
        assert process_supervisor._same_durable_owner(published, managed.token)
        assert published.payload == managed.token.payload
        if os.name == "posix":
            # POSIX publishes the launcher's exact token. The Windows helper
            # publishes before a launcher owner exists; the launcher records
            # its own logical ownership only in the returned token.
            assert published == managed.token
        else:
            assert published.owner_pid == 0
            assert published.owner_started_at == ""
        assert identity_matches(published.identity)
        if os.name == "posix":
            assert terminate(published, grace_seconds=0.1)
            managed.wait(timeout=5)
            terminated_via_persisted_token = True
    finally:
        if not terminated_via_persisted_token:
            managed.terminate(grace_seconds=0.1)
            managed.wait(timeout=5)
        metadata_path.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX adoptable publication")
def test_adoptable_durable_token_is_published_before_spawn_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    supervision_id = f"adoptable-published-{uuid.uuid4().hex}"
    metadata_path = process_supervisor.durable_metadata_path(supervision_id)
    managed = ProcessSupervisor(
        LifetimeMode.ADOPTABLE,
        supervision_id=supervision_id,
        publish_durable_token=True,
    ).spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        published = SupervisionToken.from_dict(
            json.loads(metadata_path.read_text(encoding="utf-8"))
        )
        assert published == managed.token
        assert published.mode is LifetimeMode.ADOPTABLE
        assert identity_matches(published.identity)
    finally:
        managed.terminate(grace_seconds=0.1)
        managed.wait(timeout=5)
        metadata_path.unlink(missing_ok=True)


def test_durable_token_publication_rejects_run_owned_lifetime() -> None:
    with pytest.raises(ValueError, match="requires adoptable or detached lifetime"):
        ProcessSupervisor(
            LifetimeMode.RUN_OWNED,
            publish_durable_token=True,
        )


@pytest.mark.skipif(os.name != "posix", reason="native POSIX ownership integration")
def test_detached_publication_failure_terminates_launched_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    real_popen = process_supervisor.subprocess.Popen
    launched: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            launched.append(process)
        return process

    monkeypatch.setattr(process_supervisor.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        process_supervisor,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("publication failed")),
    )

    with pytest.raises(OSError, match="publication failed"):
        ProcessSupervisor(
            LifetimeMode.DETACHED,
            supervision_id="failed-publication",
            publish_durable_token=True,
        ).spawn([sys.executable, "-c", "import time; time.sleep(30)"])

    assert len(launched) == 1
    launched[0].wait(timeout=5)
    assert launched[0].returncode is not None


def test_legacy_pid_record_rejects_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_supervisor.os, "name", "posix")
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda pid: ProcessIdentity(pid, "live"),
    )

    assert legacy_pid_record_is_live(42)
    assert not terminate_legacy_pid_record(42)
    assert not legacy_pid_record_is_live(42, "recorded")
    assert not terminate_legacy_pid_record(42, "recorded")


def test_legacy_pid_record_termination_routes_through_supervision_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = ProcessIdentity(42, "created", "/usr/bin/python")
    owner = ProcessIdentity(7, "owner", "/usr/bin/python")
    monkeypatch.setattr(process_supervisor.os, "name", "posix")
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda pid: process if pid == process.pid else owner,
    )
    monkeypatch.setattr(
        process_supervisor.os,
        "getpgid",
        lambda pid: pid,
        raising=False,
    )
    terminate_call: list[tuple[SupervisionToken, float]] = []

    def fake_terminate(token: SupervisionToken, *, grace_seconds: float = 5.0) -> bool:
        terminate_call.append((token, grace_seconds))
        return True

    monkeypatch.setattr(process_supervisor, "terminate", fake_terminate)

    assert legacy_pid_record_is_live(process.pid, process.started_at)
    assert terminate_legacy_pid_record(
        process.pid,
        process.started_at,
        grace_seconds=0.25,
    )
    token, grace = terminate_call[0]
    assert token.identity == process
    assert token.pgid == process.pid
    assert token.version == 1
    assert grace == 0.25


@pytest.mark.skipif(os.name != "posix", reason="native POSIX ownership integration")
def test_legacy_raw_popen_without_owned_group_fails_closed() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: S603
    try:
        assert process_supervisor.terminate_legacy_popen_tree(process, grace_seconds=0) is False
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="native POSIX current-process ownership integration")
def test_posix_claimed_current_process_token_stops_complete_group(tmp_path: Path) -> None:
    state_path = tmp_path / "claimed.json"
    helper = tmp_path / "claimed-helper.py"
    helper.write_text(
        "import json,signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "from spec_runtime.process_supervisor import claim_current_process\n"
        "token=claim_current_process('claimed-posix-test')\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "child_ready=Path(sys.argv[1]+'.child-ready')\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(\"ready\"); time.sleep(30)',str(child_ready)])\n"
        "while not child_ready.exists(): time.sleep(.02)\n"
        "Path(sys.argv[1]).write_text(json.dumps({'token':token.to_dict(),'child_pid':child.pid}))\n"
        "while True: time.sleep(.05)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, str(helper), str(state_path)])  # noqa: S603
    token: SupervisionToken | None = None
    try:
        deadline = time.monotonic() + 10
        while not state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state_path.exists()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        token = SupervisionToken.from_dict(payload["token"])
        assert token.pgid == token.identity.pid == process.pid
        assert token.version == 1
        assert token.job_name == ""
        assert token.control_relpath == ""
        assert token == SupervisionToken.from_dict(token.to_dict())

        assert process_supervisor.stop_supervised_process(
            token,
            grace_seconds=0.1,
            hard_grace_seconds=2,
        ) is True
        process.wait(timeout=5)
        assert process_supervisor.is_process_group_alive(token.pgid) is False
        assert inspect_process(int(payload["child_pid"])) is None
    finally:
        if token is not None and process_supervisor.is_process_group_alive(token.pgid):
            os.killpg(token.pgid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="native POSIX orphan-group integration")
def test_legacy_orphaned_group_with_live_descendant_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "orphan.json"
    exit_path = tmp_path / "exit"
    helper = tmp_path / "orphan-helper.py"
    helper.write_text(
        "import json,os,signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "os.setpgrp()\n"
        "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
        "Path(sys.argv[1]).write_text(json.dumps({'child_pid':child.pid}))\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.02)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, str(helper), str(state_path), str(exit_path)])  # noqa: S603
    identity: ProcessIdentity | None = None
    try:
        deadline = time.monotonic() + 10
        while not state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state_path.exists()
        identity = inspect_process(process.pid)
        assert identity is not None
        exit_path.write_text("exit", encoding="utf-8")
        process.wait(timeout=5)
        assert process_supervisor.is_process_group_alive(process.pid) is True

        with pytest.raises(process_supervisor.ProcessGroupOwnershipError, match="orphaned or reused"):
            process_supervisor.terminate_legacy_process_group(
                process.pid,
                process.pid,
                identity.started_at,
                grace_seconds=0,
            )
        assert inspect_process(int(json.loads(state_path.read_text())["child_pid"])) is not None
    finally:
        if process_supervisor.is_process_group_alive(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group identity policy")
def test_legacy_reused_leader_identity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_supervisor, "is_process_group_alive", lambda _pgid: True)
    monkeypatch.setattr(
        process_supervisor,
        "inspect_process",
        lambda pid: ProcessIdentity(pid, "different-start"),
    )
    monkeypatch.setattr(
        process_supervisor.os,
        "killpg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must fail closed")),
    )

    with pytest.raises(process_supervisor.ProcessGroupOwnershipError, match="orphaned or reused"):
        process_supervisor.terminate_legacy_process_group(42, 42, "recorded-start")


@pytest.mark.skipif(os.name != "posix", reason="legacy SIGTERM compatibility is POSIX-only")
def test_legacy_single_process_shutdown_revalidates_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(4242, "created")
    signal_process = MagicMock()
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda candidate: candidate == identity)
    monkeypatch.setattr(process_supervisor.os, "kill", signal_process)

    assert process_supervisor.request_legacy_process_shutdown(identity)
    signal_process.assert_called_once_with(identity.pid, signal.SIGTERM)

    signal_process.reset_mock()
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _candidate: False)
    assert not process_supervisor.request_legacy_process_shutdown(identity)
    signal_process.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="exact PID termination is POSIX-only")
def test_exact_process_termination_revalidates_before_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(4242, "created")
    send_signal = MagicMock()
    close_handle = MagicMock()
    monkeypatch.setattr(process_supervisor, "_open_verified_posix_process_handle", lambda _identity: 17)
    monkeypatch.setattr(process_supervisor, "_signal_posix_process_handle", send_signal)
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _candidate: False)
    monkeypatch.setattr(process_supervisor.os, "close", close_handle)

    assert process_supervisor.terminate_exact_process(identity, grace_seconds=0)
    send_signal.assert_called_once_with(17, signal.SIGTERM)
    close_handle.assert_called_once_with(17)


@pytest.mark.skipif(os.name != "posix", reason="exact PID termination is POSIX-only")
def test_exact_process_termination_escalates_only_same_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(4242, "created")
    send_signal = MagicMock(return_value=True)
    close_handle = MagicMock()
    monkeypatch.setattr(process_supervisor, "_open_verified_posix_process_handle", lambda _identity: 17)
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda candidate: candidate == identity)
    monkeypatch.setattr(process_supervisor, "_signal_posix_process_handle", send_signal)
    monkeypatch.setattr(process_supervisor.os, "close", close_handle)

    assert not process_supervisor.terminate_exact_process(
        identity,
        grace_seconds=0,
        hard_grace_seconds=0,
    )
    assert send_signal.call_args_list == [
        call(17, signal.SIGTERM),
        call(17, signal.SIGKILL),
    ]
    close_handle.assert_called_once_with(17)


@pytest.mark.skipif(os.name != "posix", reason="exact PID termination is POSIX-only")
def test_exact_process_termination_without_stable_handle_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(4242, "created")
    signal_process = MagicMock()
    monkeypatch.setattr(
        process_supervisor,
        "_open_verified_posix_process_handle",
        lambda _identity: None,
    )
    monkeypatch.setattr(process_supervisor.os, "kill", signal_process)

    assert not process_supervisor.terminate_exact_process(identity)
    signal_process.assert_not_called()


def test_adoptable_token_records_new_logical_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = ProcessIdentity(42, "created", "python.exe")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, identity, 7, "owner", "unique-adoption-token")
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    control_path = tmp_path / token.control_relpath
    control_path.parent.mkdir(parents=True)
    control_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "supervision_id": token.token,
                "job_name": token.job_name,
                "nonce": token.control_nonce,
                "keeper_identity": token.identity.to_dict(),
                "payload_identity": token.payload.to_dict(),
                "adopted_by": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("spec_runtime.process_supervisor.identity_matches", lambda _identity: True)
    monkeypatch.setattr(
        "spec_runtime.process_supervisor.inspect_process",
        lambda pid: ProcessIdentity(pid, "new-owner", sys.executable),
    )
    assert adopt(token).owner_pid == os.getpid()
    state = json.loads(control_path.read_text(encoding="utf-8"))
    assert state["adoption_generation"] == 1
    assert state["adopted_by"]["pid"] == os.getpid()
    with pytest.raises(ValueError, match="already adopted"):
        adopt(token)


def test_token_distinguishes_supervisor_and_payload_identity() -> None:
    helper = ProcessIdentity(41, "helper")
    payload = ProcessIdentity(42, "payload")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, helper, 7, "owner", uuid.uuid4().hex, 0, payload)
    restored = SupervisionToken.from_dict(token.to_dict())
    assert restored.identity == helper
    assert restored.payload == payload


def test_token_persists_explicit_job_name() -> None:
    keeper = ProcessIdentity(41, "helper")
    payload = ProcessIdentity(42, "payload")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "supervision-id",
        payload_identity=payload,
        job_name=r"Global\SpecButler-supervision-id",
    )
    restored = SupervisionToken.from_dict(token.to_dict())
    assert restored.identity == keeper
    assert restored.payload == payload
    assert restored.job_name == token.job_name


def test_managed_process_keeps_live_job_registered_when_close_handle_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "close-failure")

    class FailingJob:
        def close(self) -> None:
            raise OSError("CloseHandle failed")

    job = FailingJob()
    key = (identity.pid, identity.started_at)
    monkeypatch.setitem(process_supervisor._LIVE_WINDOWS_JOBS, key, job)
    managed = process_supervisor.ManagedProcess(object(), token, job)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="CloseHandle failed"):
        managed.close()

    assert process_supervisor._LIVE_WINDOWS_JOBS[key] is job
    assert managed._job is job


def test_windows_job_close_is_serialized_and_idempotent_across_callers() -> None:
    close_entered = threading.Event()
    release_close = threading.Event()
    calls: list[int] = []

    class Kernel:
        def CloseHandle(self, handle: int) -> bool:
            calls.append(handle)
            close_entered.set()
            assert release_close.wait(timeout=5)
            return True

    job = process_supervisor._WindowsJob.__new__(process_supervisor._WindowsJob)
    job._kernel32 = Kernel()
    job._handle_lock = threading.RLock()
    job.handle = 1234

    first = threading.Thread(target=job.close)
    second = threading.Thread(target=job.close)
    first.start()
    assert close_entered.wait(timeout=5)
    second.start()
    try:
        # The second caller must wait outside CloseHandle while the first owns
        # the wrapper lock. Without intrinsic serialization this becomes two
        # closes of the same numeric handle.
        time.sleep(0.05)
        assert calls == [1234]
    finally:
        release_close.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [1234]
    assert job.handle is None


def test_bind_held_windows_job_payload_requires_kernel_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = ProcessIdentity(41, "leader-created")
    candidate = ProcessIdentity(42, "service-created", command="service")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        leader,
        7,
        "owner",
        "setup-handoff",
        pgid=41,
    )

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            return identity == candidate

    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setitem(
        process_supervisor._LIVE_WINDOWS_JOBS,
        (leader.pid, leader.started_at),
        Job(),
    )

    bound = process_supervisor.bind_held_windows_job_payload(token, candidate)

    assert bound is not None
    assert bound.identity == leader
    assert bound.payload == candidate
    assert (
        process_supervisor.bind_held_windows_job_payload(
            token,
            ProcessIdentity(99, "other"),
        )
        is None
    )


def test_windows_run_owned_boundary_is_inactive_when_named_job_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keeper = ProcessIdentity(41, "keeper-created")
    payload = ProcessIdentity(42, "payload-created")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "completed-boundary",
        payload_identity=payload,
    )
    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    absent = MagicMock(return_value=True)
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", absent)

    assert process_supervisor.supervision_boundary_is_inactive(token)
    absent.assert_called_once_with(token.job_name)


def test_windows_run_owned_boundary_retirement_fails_closed_without_absence_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keeper = ProcessIdentity(41, "keeper-created")
    payload = ProcessIdentity(42, "payload-created")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "unproven-boundary",
        payload_identity=payload,
    )
    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        process_supervisor,
        "_windows_job_definitively_absent",
        lambda _name: False,
    )

    assert not process_supervisor.supervision_boundary_is_inactive(token)


@pytest.mark.parametrize("members", [(), (99,)])
def test_windows_run_owned_boundary_uses_retained_job_inventory(
    monkeypatch: pytest.MonkeyPatch,
    members: tuple[int, ...],
) -> None:
    keeper = ProcessIdentity(41, "keeper-created")
    payload = ProcessIdentity(42, "payload-created")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        f"held-boundary-{len(members)}",
        payload_identity=payload,
    )

    class Job:
        def active_process_ids(self) -> tuple[int, ...]:
            return members

    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setitem(
        process_supervisor._LIVE_WINDOWS_JOBS,
        (keeper.pid, keeper.started_at),
        Job(),
    )
    absent = MagicMock(side_effect=AssertionError("held Job bypassed"))
    monkeypatch.setattr(process_supervisor, "_windows_job_definitively_absent", absent)

    assert process_supervisor.supervision_boundary_is_inactive(token) is (not members)
    absent.assert_not_called()


def test_windows_run_owned_boundary_preserves_token_on_job_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keeper = ProcessIdentity(41, "keeper-created")
    payload = ProcessIdentity(42, "payload-created")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "query-failure-boundary",
        payload_identity=payload,
    )

    class Job:
        def active_process_ids(self) -> tuple[int, ...]:
            raise OSError("access denied")

    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setitem(
        process_supervisor._LIVE_WINDOWS_JOBS,
        (keeper.pid, keeper.started_at),
        Job(),
    )

    assert not process_supervisor.supervision_boundary_is_inactive(token)


@pytest.mark.parametrize("members", [[], [99], None])
def test_posix_run_owned_boundary_requires_successful_empty_group_inventory(
    monkeypatch: pytest.MonkeyPatch,
    members: list[int] | None,
) -> None:
    keeper = ProcessIdentity(41, "keeper-created")
    payload = ProcessIdentity(42, "payload-created")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "posix-completed-boundary",
        pgid=41,
        payload_identity=payload,
    )
    monkeypatch.setattr(process_supervisor.os, "name", "posix")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        process_supervisor,
        "list_live_process_group_members",
        lambda _pgid: members,
    )

    assert process_supervisor.supervision_boundary_is_inactive(token) is (members == [])


def test_posix_run_owned_boundary_rejects_mismatched_keeper_and_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keeper = ProcessIdentity(41, "keeper-created")
    payload = ProcessIdentity(42, "payload-created")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "corrupt-posix-boundary",
        pgid=99,
        payload_identity=payload,
    )
    monkeypatch.setattr(process_supervisor.os, "name", "posix")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    inventory = MagicMock(side_effect=AssertionError("mismatched group inventoried"))
    monkeypatch.setattr(
        process_supervisor,
        "list_live_process_group_members",
        inventory,
    )

    assert not process_supervisor.supervision_boundary_is_inactive(token)
    inventory.assert_not_called()


def test_managed_process_wait_closes_job_after_leader_exit() -> None:
    events: list[str] = []
    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "normal-wait")

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            return 23

    class Job:
        def close(self) -> None:
            events.append("close")

    managed = process_supervisor.ManagedProcess(Process(), token, Job())  # type: ignore[arg-type]
    assert managed.wait(timeout=2.0) == 23
    assert events == ["wait:2.0", "close"]
    assert managed._job is None


def test_managed_process_starts_only_one_pipe_closer_across_timeout_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "one-closer")
    started: list[object] = []

    class Thread:
        def __init__(self, *, target: object, daemon: bool) -> None:
            assert daemon is True
            self.target = target

        def start(self) -> None:
            started.append(self.target)

    monkeypatch.setattr(process_supervisor.threading, "Thread", Thread)
    managed = process_supervisor.ManagedProcess(object(), token, object())  # type: ignore[arg-type]

    managed._start_pipe_closer_once()
    managed._start_pipe_closer_once()
    managed._start_pipe_closer_once()

    assert started == [managed._close_job_after_leader]


def test_managed_async_process_starts_only_one_pipe_closer_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "one-async-closer")
    started: list[object] = []

    monkeypatch.setattr(
        process_supervisor.asyncio,
        "create_task",
        lambda coroutine: started.append(coroutine) or MagicMock(),
    )
    managed = process_supervisor.ManagedAsyncProcess(object(), token, object())  # type: ignore[arg-type]

    managed._start_pipe_closer_once()
    managed._start_pipe_closer_once()

    assert len(started) == 1
    started[0].close()  # type: ignore[union-attr]


def test_managed_process_communicate_preserves_baseexception_when_cleanup_fails() -> None:
    class Abort(BaseException):
        pass

    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "sync-abort")

    class Process:
        def communicate(self, **_kwargs: object) -> tuple[None, None]:
            raise Abort

    class Job:
        def terminate(self) -> None:
            raise ValueError("terminate cleanup failed")

        def close(self) -> None:
            raise ValueError("close cleanup failed")

    managed = process_supervisor.ManagedProcess(Process(), token, Job())  # type: ignore[arg-type]
    with pytest.raises(Abort):
        managed.communicate()


def test_managed_async_communicate_preserves_baseexception_when_cleanup_fails() -> None:
    class Abort(BaseException):
        pass

    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "async-abort")

    class Process:
        async def communicate(self, _input: bytes | None) -> tuple[None, None]:
            raise Abort

        async def wait(self) -> int:
            raise ValueError("wait cleanup failed")

    class Job:
        def terminate(self) -> None:
            raise ValueError("terminate cleanup failed")

        def close(self) -> None:
            raise ValueError("close cleanup failed")

    async def exercise() -> None:
        managed = process_supervisor.ManagedAsyncProcess(Process(), token, Job())  # type: ignore[arg-type]
        with pytest.raises(Abort):
            await managed.communicate()

    asyncio.run(exercise())


def test_held_windows_job_uses_original_group_and_exact_grace_before_hard_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    identity = ProcessIdentity(42, "exited-shim")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        identity,
        7,
        "owner",
        "held-job-grace",
        pgid=4242,
    )

    class Job:
        def active_process_ids(self) -> tuple[int, ...]:
            return (99,)

        def wait_empty(self, timeout: float) -> bool:
            events.append(("wait", timeout))
            return False

        def active_identities(self) -> list[ProcessIdentity]:
            return []

        def terminate(self) -> None:
            events.append("hard-kill")

    monkeypatch.setattr(process_supervisor, "_send_windows_break", lambda pgid: events.append(("break", pgid)) or True)

    assert process_supervisor._terminate_held_windows_job(token, Job(), 0.375) is True  # type: ignore[arg-type]
    assert events == [("break", 4242), ("wait", 0.375), "hard-kill"]


def test_durable_metadata_path_is_independent_of_payload_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    control_root = tmp_path / "writable-controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(control_root))
    monkeypatch.chdir(tmp_path)

    path = process_supervisor.durable_metadata_path("stable-id")

    assert path == control_root / "metadata" / "stable-id.json"
    assert path.parent != tmp_path


def _write_promotion_state(tmp_path: Path, token: SupervisionToken) -> None:
    control_path = tmp_path / token.control_relpath
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "supervision_id": token.token,
                "job_name": token.job_name,
                "nonce": token.control_nonce,
                "keeper_identity": token.identity.to_dict(),
                "payload_identity": token.payload.to_dict(),
                "request": None,
            }
        ),
        encoding="utf-8",
    )
    metadata_path = process_supervisor.durable_metadata_path(token.token)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(token.to_dict()), encoding="utf-8")


def test_payload_promotion_updates_locked_control_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    shim = ProcessIdentity(42, "shim", "python.exe")
    candidate = ProcessIdentity(43, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "promotion-positive",
        payload_identity=shim,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            return identity == candidate

        def close(self) -> None:
            pass

    monkeypatch.setattr(process_supervisor, "identity_matches", lambda identity: identity != shim)
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", classmethod(lambda _cls, _name: Job()))

    promoted = promote_payload_identity(token, candidate)

    assert promoted.payload == candidate
    control = json.loads((tmp_path / token.control_relpath).read_text(encoding="utf-8"))
    metadata = SupervisionToken.from_dict(
        json.loads(process_supervisor.durable_metadata_path(token.token).read_text(encoding="utf-8"))
    )
    assert control["payload_identity"] == candidate.to_dict()
    assert metadata.payload == candidate


def test_payload_promotion_rejects_candidate_from_another_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    shim = ProcessIdentity(42, "shim", "python.exe")
    foreign = ProcessIdentity(99, "foreign", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "promotion-cross-job",
        payload_identity=shim,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)

    class Job:
        def contains(self, _identity: ProcessIdentity) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: True)
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", classmethod(lambda _cls, _name: Job()))

    with pytest.raises(ValueError, match="not an active member"):
        promote_payload_identity(token, foreign)

    control = json.loads((tmp_path / token.control_relpath).read_text(encoding="utf-8"))
    assert control["payload_identity"] == shim.to_dict()


def test_persisted_windows_termination_keeps_authenticated_job_when_payload_exits_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    payload = ProcessIdentity(42, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "payload-exit-at-deadline",
        payload_identity=payload,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)
    events: list[object] = []

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            events.append(("contains", identity))
            # Initial authentication succeeds. A second membership check
            # would model the racy, already-exited payload and must not occur.
            return len([event for event in events if isinstance(event, tuple)]) == 1

        def active_process_ids(self) -> tuple[int, ...]:
            return (99,)

        def active_identities(self) -> list[ProcessIdentity]:
            return [ProcessIdentity(99, "remaining-job-member")]

        def terminate(self) -> None:
            events.append("terminate")

        def close(self) -> None:
            events.append("close")

    job = Job()
    monkeypatch.setattr(process_supervisor, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: True)
    monkeypatch.setattr(
        process_supervisor._WindowsJob,
        "open",
        classmethod(lambda _cls, _name: job),
    )
    monkeypatch.setattr(process_supervisor, "_wait_for_identities_exit", lambda _items: True)

    assert process_supervisor.terminate(token, grace_seconds=0)
    assert events == [("contains", payload), "terminate", "close"]


def test_persisted_windows_termination_accepts_empty_job_after_keeper_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    payload = ProcessIdentity(42, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "empty-job-after-keeper-exit",
        payload_identity=payload,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)
    events: list[object] = []
    identity_checks = 0

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            events.append(("contains", identity))
            return True

        def active_process_ids(self) -> tuple[int, ...]:
            events.append("empty")
            return ()

        def active_identities(self) -> list[ProcessIdentity]:
            raise AssertionError("an empty Job must not be escalated")

        def terminate(self) -> None:
            raise AssertionError("an empty Job must not be terminated")

        def close(self) -> None:
            events.append("close")

    def identity_matches_then_exits(_identity: ProcessIdentity) -> bool:
        nonlocal identity_checks
        identity_checks += 1
        return identity_checks == 1

    monkeypatch.setattr(process_supervisor, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(process_supervisor, "identity_matches", identity_matches_then_exits)
    monkeypatch.setattr(
        process_supervisor._WindowsJob,
        "open",
        classmethod(lambda _cls, _name: Job()),
    )

    assert process_supervisor.terminate(token, grace_seconds=0)
    assert identity_checks == 1
    assert events == [("contains", payload), "empty", "close"]


def test_persisted_windows_termination_rejects_nonempty_job_after_keeper_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    payload = ProcessIdentity(42, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "nonempty-job-after-keeper-exit",
        payload_identity=payload,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)
    events: list[object] = []
    identity_checks = 0

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            events.append(("contains", identity))
            return True

        def active_process_ids(self) -> tuple[int, ...]:
            events.append("nonempty")
            return (99,)

        def active_identities(self) -> list[ProcessIdentity]:
            raise AssertionError("lost keeper identity must prevent escalation")

        def terminate(self) -> None:
            raise AssertionError("lost keeper identity must prevent termination")

        def close(self) -> None:
            events.append("close")

    def keeper_exits_after_authentication(_identity: ProcessIdentity) -> bool:
        nonlocal identity_checks
        identity_checks += 1
        return identity_checks == 1

    monkeypatch.setattr(process_supervisor, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(process_supervisor, "identity_matches", keeper_exits_after_authentication)
    monkeypatch.setattr(
        process_supervisor._WindowsJob,
        "open",
        classmethod(lambda _cls, _name: Job()),
    )

    assert not process_supervisor.terminate(token, grace_seconds=0)
    assert identity_checks == 2
    assert events == [
        ("contains", payload),
        "nonempty",
        "nonempty",
        "close",
    ]


def test_payload_promotion_converges_after_death_between_atomic_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    keeper = ProcessIdentity(41, "keeper", "python.exe")
    shim = ProcessIdentity(42, "shim", "python.exe")
    candidate = ProcessIdentity(43, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "promotion-crash-convergence",
        payload_identity=shim,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            return identity == candidate

        def close(self) -> None:
            pass

    monkeypatch.setattr(process_supervisor, "identity_matches", lambda identity: identity != shim)
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", classmethod(lambda _cls, _name: Job()))
    real_atomic_write = process_supervisor.atomic_write_text
    write_count = 0

    def die_on_metadata(path: Path, content: str) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise SimulatedProcessDeath
        real_atomic_write(path, content)

    monkeypatch.setattr(process_supervisor, "atomic_write_text", die_on_metadata)
    with pytest.raises(SimulatedProcessDeath):
        promote_payload_identity(token, candidate)

    control = json.loads((tmp_path / token.control_relpath).read_text(encoding="utf-8"))
    metadata = SupervisionToken.from_dict(
        json.loads(process_supervisor.durable_metadata_path(token.token).read_text(encoding="utf-8"))
    )
    assert control["payload_identity"] == candidate.to_dict()
    assert metadata.payload == shim

    monkeypatch.setattr(process_supervisor, "atomic_write_text", real_atomic_write)
    assert promote_payload_identity(token, candidate).payload == candidate


def test_durable_helper_retires_only_matching_authenticated_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keeper = inspect_process(os.getpid())
    assert keeper is not None
    payload = ProcessIdentity(42, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "retire-matching-records",
        payload_identity=payload,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)
    metadata_path = process_supervisor.durable_metadata_path(token.token)
    process_supervisor._acknowledge_durable_publication(metadata_path, token)

    assert process_supervisor._retire_durable_records(metadata_path, token)
    assert not metadata_path.exists()
    assert metadata_path.parent.is_dir()
    assert not (tmp_path / token.control_relpath).exists()
    assert not process_supervisor._durable_publication_ack_path(metadata_path).exists()
    assert not (tmp_path / token.control_relpath).with_suffix(".lock").exists()


def test_durable_helper_refuses_to_retire_mismatched_control_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keeper = inspect_process(os.getpid())
    assert keeper is not None
    token = SupervisionToken(
        LifetimeMode.ADOPTABLE,
        keeper,
        7,
        "owner",
        "retire-mismatch",
        payload_identity=ProcessIdentity(42, "payload", "python.exe"),
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)
    control_path = tmp_path / token.control_relpath
    control = json.loads(control_path.read_text(encoding="utf-8"))
    control["nonce"] = "different-owner"
    control_path.write_text(json.dumps(control), encoding="utf-8")
    metadata_path = process_supervisor.durable_metadata_path(token.token)

    assert not process_supervisor._retire_durable_records(metadata_path, token)
    assert metadata_path.exists()
    assert control_path.exists()


def test_durable_helper_cleanup_never_masks_payload_baseexception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class PayloadAbort(BaseException):
        pass

    keeper = ProcessIdentity(41, "keeper", "python.exe")
    payload = ProcessIdentity(42, "payload", "python.exe")
    child_token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        payload,
        7,
        "owner",
        "helper-baseexception",
    )

    class Child:
        token = child_token

        def owned_tree_active(self) -> bool:
            return False

        def wait(self) -> int:
            raise PayloadAbort

    class Supervisor:
        def __enter__(self) -> Supervisor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def spawn(self, _argv: object) -> Child:
            return Child()

    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setattr(
        process_supervisor,
        "ProcessSupervisor",
        lambda *_args, **_kwargs: Supervisor(),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_stabilize_windows_payload_identity",
        lambda _child: payload,
    )
    monkeypatch.setattr(process_supervisor, "inspect_process", lambda _pid: keeper)
    monkeypatch.setattr(
        process_supervisor,
        "_await_durable_publication",
        lambda *_args: (_ for _ in ()).throw(ValueError("ack cleanup failed")),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_retire_durable_records",
        lambda *_args: (_ for _ in ()).throw(ValueError("record cleanup failed")),
    )

    with pytest.raises(PayloadAbort):
        process_supervisor._durable_helper(
            process_supervisor.durable_metadata_path(child_token.token),
            child_token.token,
            LifetimeMode.DETACHED,
            child_token.control_relpath,
            child_token.control_nonce,
            ProcessIdentity(7, "publisher"),
            [sys.executable, "-c", "pass"],
        )


def test_durable_helper_retires_control_after_metadata_publication_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class PublicationFailure(OSError):
        pass

    keeper = inspect_process(os.getpid())
    assert keeper is not None
    payload = ProcessIdentity(42, "payload", "python.exe")
    supervision_id = "partial-publication"
    child_token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        payload,
        keeper.pid,
        keeper.started_at,
        supervision_id,
    )

    class Child:
        token = child_token
        returncode = 0

        def owned_tree_active(self) -> bool:
            return False

        def wait(self) -> int:
            return 0

    class Supervisor:
        def __enter__(self) -> Supervisor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def spawn(self, _argv: object) -> Child:
            return Child()

    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    metadata_path = process_supervisor.durable_metadata_path(supervision_id)
    control_relpath = f"controls/{supervision_id}/control.json"
    control_path = tmp_path / control_relpath
    control_nonce = "partial-publication-nonce"
    real_atomic_write = process_supervisor.atomic_write_text

    def fail_metadata_publication(path: Path, content: str) -> None:
        if path == metadata_path:
            raise PublicationFailure("metadata publication failed")
        real_atomic_write(path, content)

    monkeypatch.setattr(
        process_supervisor,
        "ProcessSupervisor",
        lambda *_args, **_kwargs: Supervisor(),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_stabilize_windows_payload_identity",
        lambda _child: payload,
    )
    monkeypatch.setattr(process_supervisor, "atomic_write_text", fail_metadata_publication)

    with pytest.raises(PublicationFailure):
        process_supervisor._durable_helper(
            metadata_path,
            supervision_id,
            LifetimeMode.DETACHED,
            control_relpath,
            control_nonce,
            keeper,
            [sys.executable, "-c", "pass"],
        )

    assert not metadata_path.exists()
    assert not control_path.exists()
    assert not control_path.with_suffix(".lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object integration")
@pytest.mark.parametrize("action", ["normal", "stop", "owner-close"])
def test_windows_run_owned_parent_child_grandchild_tree(tmp_path: Path, action: str) -> None:
    """Exercise real descendants; no process API is mocked in this test."""
    pid_file = tmp_path / "pids.json"
    script = tmp_path / "tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30 if level else 2)\n",
        encoding="utf-8",
    )
    supervisor = ProcessSupervisor(LifetimeMode.RUN_OWNED)
    managed = supervisor.spawn([sys.executable, str(script), "0", str(pid_file)])
    while not all(Path(f"{pid_file}-{level}").exists() for level in range(3)):
        time.sleep(0.05)
    if action == "normal":
        managed.wait(timeout=10)
    else:
        if action == "owner-close":
            supervisor.close()
        else:
            managed.terminate(grace_seconds=0.1)
        managed.wait(timeout=10)
    for level in range(3):
        pid = int(Path(f"{pid_file}-{level}").read_text(encoding="utf-8"))
        assert inspect_process(pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows current-process Job integration")
def test_windows_claimed_current_process_token_stops_from_another_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    state_path = tmp_path / "claimed-token.json"
    stopped_path = tmp_path / "stopped"
    helper = tmp_path / "claim-current.py"
    stopper = tmp_path / "stop-claimed.py"
    supervision_id = f"current-{uuid.uuid4().hex}"
    helper.write_text(
        "import json,os,signal,sys,time\n"
        "from pathlib import Path\n"
        "from spec_runtime.process_supervisor import claim_current_process\n"
        "token=claim_current_process(sys.argv[1])\n"
        "def stop(*_args):\n"
        "    Path(sys.argv[3]).write_text('stopped')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[2]).write_text(json.dumps({'helper_pid':os.getpid(),'token':token.to_dict()}))\n"
        "while True: time.sleep(.05)\n",
        encoding="utf-8",
    )
    stopper.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "from spec_runtime.process_supervisor import SupervisionToken,stop_supervised_process\n"
        "payload=json.loads(Path(sys.argv[1]).read_text())\n"
        "token=SupervisionToken.from_dict(payload['token'])\n"
        "raise SystemExit(0 if stop_supervised_process(token,grace_seconds=3) else 1)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(helper), supervision_id, str(state_path), str(stopped_path)],
        env=os.environ.copy(),
    )
    token: SupervisionToken | None = None
    try:
        deadline = time.monotonic() + 10
        while not state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state_path.exists()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        token = SupervisionToken.from_dict(payload["token"])
        helper_pid = int(payload["helper_pid"])
        assert token.version == 2
        assert helper_pid == token.identity.pid
        assert helper_pid != os.getpid()
        assert token.job_name == f"Global\\SpecButler-{supervision_id}"
        assert token.control_relpath

        completed = subprocess.run(  # noqa: S603
            [sys.executable, str(stopper), str(state_path)],
            env=os.environ.copy(),
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0
        process.wait(timeout=10)
        assert process.returncode is not None
        assert stopped_path.read_text(encoding="utf-8") == "stopped"
        assert inspect_process(helper_pid) is None
    finally:
        if (
            token is not None
            and token.identity.pid != os.getpid()
            and inspect_process(token.identity.pid) is not None
        ):
            process_supervisor.stop_supervised_process(token, grace_seconds=0)
        if process.pid != os.getpid() and process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="native Windows current-process cleanup")
def test_windows_current_process_claim_retires_records_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    supervision_id = f"normal-exit-{uuid.uuid4().hex}"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from spec_runtime.process_supervisor import claim_current_process; "
                "claim_current_process(sys.argv[1])"
            ),
            supervision_id,
        ],
        env=os.environ.copy(),
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    control_path = root / "controls" / supervision_id / "control.json"
    assert not control_path.exists()
    assert not control_path.with_suffix(".lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows crash reconciliation")
def test_windows_abrupt_current_process_claim_is_reconciled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    supervision_id = f"abrupt-exit-{uuid.uuid4().hex}"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "from spec_runtime.process_supervisor import claim_current_process; "
                "claim_current_process(sys.argv[1]); os._exit(0)"
            ),
            supervision_id,
        ],
        env=os.environ.copy(),
        check=False,
        timeout=10,
    )
    control_path = root / "controls" / supervision_id / "control.json"

    assert completed.returncode == 0
    assert control_path.exists()
    assert process_supervisor.reconcile_stale_control_state() == 1
    assert not control_path.exists()
    assert not control_path.with_suffix(".lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows timeout integration")
def test_windows_run_timeout_kills_parent_child_grandchild_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout-pids"
    script = tmp_path / "timeout-tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, str(script), "0", str(pid_file)], timeout=1, capture_output=True)
    for level in range(3):
        path = Path(f"{pid_file}-{level}")
        assert path.exists()
        assert inspect_process(int(path.read_text(encoding="utf-8"))) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows owner death integration")
def test_windows_external_owner_death_closes_job_and_kills_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "owner-death-pids"
    tree = tmp_path / "owner-death-tree.py"
    tree.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "owner.py"
    launcher.write_text(
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from spec_runtime.process_supervisor import LifetimeMode,ProcessSupervisor\n"
        "ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn([sys.executable,sys.argv[1],'0',sys.argv[2]])\n"
        "paths=[Path(sys.argv[2]+'-'+str(i)) for i in range(3)]\n"
        "while not all(path.exists() for path in paths): time.sleep(.05)\n"
        "os._exit(17)\n",
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, str(launcher), str(tree), str(pid_file)], check=False, timeout=10)
    assert completed.returncode == 17
    deadline = time.monotonic() + 10
    identities = [int(Path(f"{pid_file}-{level}").read_text(encoding="utf-8")) for level in range(3)]
    while any(inspect_process(pid) is not None for pid in identities) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(inspect_process(pid) is None for pid in identities)


@pytest.mark.skipif(os.name != "nt", reason="native Windows graceful cancellation integration")
def test_windows_stop_attempts_graceful_break_before_job_termination(tmp_path: Path) -> None:
    marker = tmp_path / "graceful"
    ready = tmp_path / "ready"
    code = (
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGBREAK,lambda *_:(Path(sys.argv[1]).write_text('graceful'),sys.exit(0))); "
        "Path(sys.argv[2]).write_text('ready'); "
        "time.sleep(30)"
    )
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", code, str(marker), str(ready)]
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    managed.terminate(grace_seconds=3)
    managed.wait(timeout=10)
    # GenerateConsoleCtrlEvent is explicitly best-effort: Windows may accept
    # the request yet suppress it in inherited/redirector console topologies.
    # When delivery is supported the handler proves it ran; either way the
    # bounded Job fallback must leave the complete owned tree dead.
    if marker.exists():
        assert marker.read_text(encoding="utf-8") == "graceful"
    assert inspect_process(managed.token.identity.pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows stale identity integration")
def test_windows_rejects_stale_identity_without_signaling_live_process() -> None:
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    stale = SupervisionToken(
        managed.token.mode,
        ProcessIdentity(managed.token.identity.pid, "recycled", managed.token.identity.executable),
        managed.token.owner_pid,
        managed.token.owner_started_at,
        managed.token.token,
        payload_identity=managed.token.payload,
    )
    try:
        assert terminate(stale, grace_seconds=0) is False
        assert identity_matches(managed.token.identity)
    finally:
        managed.kill()
        managed.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="native Windows inherited pipe integration")
@pytest.mark.parametrize("timeout", [None, 2])
def test_windows_capture_closes_pipe_inherited_by_descendant(tmp_path: Path, timeout: float | None) -> None:
    script = tmp_path / "inherited-pipe.py"
    script.write_text(
        "import subprocess,sys\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        "print('leader-exited')\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    completed = run([sys.executable, str(script)], capture_output=True, text=True, timeout=timeout)
    assert completed.stdout.strip() == "leader-exited"
    assert time.monotonic() - started < 6


@pytest.mark.skipif(os.name != "nt", reason="native Windows async Job Object integration")
def test_windows_async_run_owned_owner_close_kills_complete_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "async-pids"
    script = tmp_path / "async-tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        supervisor = ProcessSupervisor(LifetimeMode.RUN_OWNED)
        managed = await supervisor.spawn_async([sys.executable, str(script), "0", str(pid_file)])
        while not all(Path(f"{pid_file}-{level}").exists() for level in range(3)):
            await asyncio.sleep(0.05)
        supervisor.close()
        await asyncio.wait_for(managed.wait(), timeout=10)

    asyncio.run(exercise())
    for level in range(3):
        pid = int(Path(f"{pid_file}-{level}").read_text(encoding="utf-8"))
        assert inspect_process(pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows async cancellation integration")
def test_windows_async_terminate_is_nonblocking(tmp_path: Path) -> None:
    ready = tmp_path / "async-terminate-ready"
    code = (
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGBREAK,lambda *_:None); "
        "Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
    )

    async def exercise() -> None:
        managed = await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
            [sys.executable, "-c", code, str(ready)]
        )
        while not ready.exists():
            await asyncio.sleep(0.02)
        started = time.monotonic()
        managed.terminate()
        assert time.monotonic() - started < 0.5
        assert managed._job is not None and managed._job.active_process_ids()
        managed.kill()
        await asyncio.wait_for(managed.wait(), timeout=10)

    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_sync_launch_baseexception_closes_unassigned_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    events: list[str] = []

    class Job:
        def __init__(self, _name: str) -> None:
            events.append("open")

        def terminate(self) -> None:
            events.append("terminate")
            raise ValueError("terminate cleanup failed")

        def close(self) -> None:
            events.append("close")
            raise ValueError("close cleanup failed")

    def aborting_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[object]:
        raise LaunchAbort

    monkeypatch.setattr(process_supervisor, "_WindowsJob", Job)
    monkeypatch.setattr(process_supervisor.subprocess, "Popen", aborting_popen)

    with pytest.raises(LaunchAbort):
        ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn([sys.executable, "-c", "pass"])

    assert events == ["open", "terminate", "close"]


def test_windows_sync_spawn_captures_identity_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Process:
        pid = 4242
        _handle = 17

        def kill(self) -> None:
            events.append("kill")

    class Job:
        def __init__(self, _name: str) -> None:
            events.append("open")

        def assign(self, handle: int) -> None:
            events.append(("assign", handle))

        def active_identities(self) -> list[ProcessIdentity]:
            return []

        def close(self) -> None:
            events.append("close")

    def inspect(pid: int) -> ProcessIdentity:
        events.append(("inspect", pid))
        return ProcessIdentity(pid, f"created-{pid}")

    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor, "_ensure_control_state_reconciled", lambda: None)
    monkeypatch.setattr(process_supervisor, "_WindowsJob", Job)
    monkeypatch.setattr(process_supervisor, "_REAL_POPEN_TYPE", Process)
    monkeypatch.setattr(process_supervisor.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(process_supervisor, "inspect_process", inspect)
    monkeypatch.setattr(
        process_supervisor,
        "_resume_windows_process",
        lambda handle: events.append(("resume", handle)),
    )

    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(["fast-command"])
    try:
        assert events.index(("inspect", 4242)) < events.index(("resume", 17))
    finally:
        managed.close()


def test_windows_async_spawn_captures_identity_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class TransportProcess:
        _handle = 23

    class Transport:
        def get_extra_info(self, name: str) -> TransportProcess:
            assert name == "subprocess"
            return TransportProcess()

    class Process:
        pid = 4343
        _transport = Transport()

        def kill(self) -> None:
            events.append("kill")

    class Job:
        def __init__(self, _name: str) -> None:
            events.append("open")

        def assign(self, handle: int) -> None:
            events.append(("assign", handle))

        def close(self) -> None:
            events.append("close")

    def inspect(pid: int) -> ProcessIdentity:
        events.append(("inspect", pid))
        return ProcessIdentity(pid, f"created-{pid}")

    async def create(*_args: object, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(process_supervisor.os, "name", "nt")
    monkeypatch.setattr(process_supervisor, "_ensure_control_state_reconciled", lambda: None)
    monkeypatch.setattr(process_supervisor, "_WindowsJob", Job)
    monkeypatch.setattr(process_supervisor.asyncio.subprocess, "Process", Process)
    monkeypatch.setattr(process_supervisor.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(process_supervisor, "inspect_process", inspect)
    monkeypatch.setattr(
        process_supervisor,
        "_resume_windows_process",
        lambda handle: events.append(("resume", handle)),
    )

    async def exercise() -> None:
        managed = await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
            ["fast-command"]
        )
        try:
            assert events.index(("inspect", 4343)) < events.index(("resume", 23))
        finally:
            managed.close()

    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_async_launch_baseexception_closes_unassigned_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    events: list[str] = []

    class Job:
        def __init__(self, _name: str) -> None:
            events.append("open")

        def terminate(self) -> None:
            events.append("terminate")

        def close(self) -> None:
            events.append("close")

    async def aborting_create(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        raise LaunchAbort

    monkeypatch.setattr(process_supervisor, "_WindowsJob", Job)
    monkeypatch.setattr(process_supervisor.asyncio, "create_subprocess_exec", aborting_create)

    async def exercise() -> None:
        with pytest.raises(LaunchAbort):
            await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
                [sys.executable, "-c", "pass"]
            )

    asyncio.run(exercise())
    assert events == ["open", "terminate", "close"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_sync_spawn_baseexception_closes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    launched_pid = 0
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[object]:
        nonlocal launched_pid
        process = real_popen(*args, **kwargs)
        launched_pid = process.pid
        return process

    monkeypatch.setattr(process_supervisor.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(process_supervisor, "_resume_windows_process", lambda _handle: (_ for _ in ()).throw(LaunchAbort()))
    with pytest.raises(LaunchAbort):
        ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
    deadline = time.monotonic() + 10
    while inspect_process(launched_pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert launched_pid > 0
    assert inspect_process(launched_pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_async_spawn_baseexception_closes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    launched_pid = 0
    real_create = asyncio.create_subprocess_exec

    async def recording_create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal launched_pid
        process = await real_create(*args, **kwargs)
        launched_pid = process.pid
        return process

    monkeypatch.setattr(process_supervisor.asyncio, "create_subprocess_exec", recording_create)
    monkeypatch.setattr(process_supervisor, "_resume_windows_process", lambda _handle: (_ for _ in ()).throw(LaunchAbort()))

    async def exercise() -> None:
        with pytest.raises(LaunchAbort):
            await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )

    asyncio.run(exercise())
    deadline = time.monotonic() + 10
    while inspect_process(launched_pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert launched_pid > 0
    assert inspect_process(launched_pid) is None


def _spawn_windows_durable_payload(
    tmp_path: Path,
    name: str,
) -> tuple[process_supervisor.ManagedProcess, ProcessIdentity]:
    marker = tmp_path / f"{name}.pid"
    code = (
        "import os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    managed = ProcessSupervisor(LifetimeMode.DETACHED, supervision_id=name).spawn(
        [sys.executable, "-c", code, str(marker)]
    )
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    identity = inspect_process(int(marker.read_text(encoding="utf-8")))
    assert identity is not None
    return managed, identity


def _assert_durable_records_removed(token: SupervisionToken) -> None:
    metadata_path = process_supervisor.durable_metadata_path(token.token)
    control_path = Path(os.environ["SPEC_PROCESS_CONTROL_ROOT"]) / token.control_relpath
    deadline = time.monotonic() + 10
    while (metadata_path.exists() or control_path.exists()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not metadata_path.exists()
    assert not control_path.exists()
    assert not process_supervisor._durable_publication_ack_path(metadata_path).exists()
    assert not control_path.with_suffix(".lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows durable cleanup integration")
def test_windows_fast_exit_durable_helper_publishes_before_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    supervision_id = f"fast-exit-{uuid.uuid4().hex}"

    managed = ProcessSupervisor(
        LifetimeMode.DETACHED,
        supervision_id=supervision_id,
    ).spawn([sys.executable, "-c", "pass"])

    assert managed.token.token == supervision_id
    assert managed.wait(timeout=10) == 0
    _assert_durable_records_removed(managed.token)


@pytest.mark.skipif(os.name != "nt", reason="native Windows durable cleanup integration")
@pytest.mark.parametrize("mode", [LifetimeMode.DETACHED, LifetimeMode.ADOPTABLE])
def test_windows_completed_durable_helper_retires_authenticated_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: LifetimeMode
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    supervision_id = f"cleanup-{mode.value}-{uuid.uuid4().hex}"
    managed = ProcessSupervisor(mode, supervision_id=supervision_id).spawn(
        [sys.executable, "-c", "import time; time.sleep(1)"]
    )
    metadata_path = process_supervisor.durable_metadata_path(supervision_id)
    control_path = Path(os.environ["SPEC_PROCESS_CONTROL_ROOT"]) / managed.token.control_relpath

    assert metadata_path.exists()
    assert control_path.exists()
    if mode is LifetimeMode.ADOPTABLE:
        adopt(managed.token)
    assert managed.wait(timeout=10) == 0
    _assert_durable_records_removed(managed.token)


@pytest.mark.skipif(os.name != "nt", reason="native Windows payload promotion integration")
def test_windows_promotes_real_payload_with_authenticated_job_membership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    managed, candidate = _spawn_windows_durable_payload(tmp_path, f"promote-{uuid.uuid4().hex}")
    promoted = managed.token
    try:
        promoted = promote_payload_identity(managed.token, candidate)
        assert promoted.payload == candidate
        control_path = Path(os.environ["SPEC_PROCESS_CONTROL_ROOT"]) / promoted.control_relpath
        control = json.loads(control_path.read_text(encoding="utf-8"))
        metadata = SupervisionToken.from_dict(
            json.loads(process_supervisor.durable_metadata_path(promoted.token).read_text(encoding="utf-8"))
        )
        assert control["payload_identity"] == candidate.to_dict()
        assert metadata.payload == candidate
    finally:
        terminate(promoted, grace_seconds=0.1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows cross-Job rejection integration")
def test_windows_rejects_payload_promotion_from_another_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    first, _first_candidate = _spawn_windows_durable_payload(tmp_path, f"first-{uuid.uuid4().hex}")
    second, foreign_candidate = _spawn_windows_durable_payload(tmp_path, f"second-{uuid.uuid4().hex}")
    try:
        with pytest.raises(ValueError, match="not an active member"):
            promote_payload_identity(first.token, foreign_candidate)
        metadata = SupervisionToken.from_dict(
            json.loads(process_supervisor.durable_metadata_path(first.token.token).read_text(encoding="utf-8"))
        )
        assert metadata.payload == first.token.payload
    finally:
        terminate(first.token, grace_seconds=0.1)
        terminate(second.token, grace_seconds=0.1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows persisted-token rejection integration")
def test_windows_persisted_termination_rejects_cross_job_control_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    first, first_candidate = _spawn_windows_durable_payload(tmp_path, f"terminate-a-{uuid.uuid4().hex}")
    second, second_candidate = _spawn_windows_durable_payload(tmp_path, f"terminate-b-{uuid.uuid4().hex}")
    first_token = promote_payload_identity(first.token, first_candidate)
    second_token = promote_payload_identity(second.token, second_candidate)
    control_path = Path(os.environ["SPEC_PROCESS_CONTROL_ROOT"]) / first_token.control_relpath
    lock_path = control_path.with_suffix(".lock")
    try:
        with process_supervisor.FileLock(lock_path):
            state = json.loads(control_path.read_text(encoding="utf-8"))
            state["payload_identity"] = second_candidate.to_dict()
            process_supervisor.atomic_write_text(control_path, json.dumps(state, sort_keys=True))
        assert terminate(first_token, grace_seconds=0) is False
        assert identity_matches(first_token.identity)
        assert identity_matches(second_token.identity)
    finally:
        with process_supervisor.FileLock(lock_path):
            state = json.loads(control_path.read_text(encoding="utf-8"))
            state["payload_identity"] = first_candidate.to_dict()
            process_supervisor.atomic_write_text(control_path, json.dumps(state, sort_keys=True))
        terminate(first_token, grace_seconds=0.1)
        terminate(second_token, grace_seconds=0.1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows durable helper integration")
def test_windows_adoptable_helper_survives_launcher_and_stops(tmp_path: Path) -> None:
    marker = tmp_path / "alive"
    token_file = tmp_path / "adoptable.json"
    launcher = tmp_path / "adoptable-launcher.py"
    launcher.write_text(
        "import json,sys\n"
        "from spec_runtime.process_supervisor import LifetimeMode,ProcessSupervisor\n"
        "payload=[sys.executable,'-c',"
        "'from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text(\"alive\"); time.sleep(30)',sys.argv[2]]\n"
        "managed=ProcessSupervisor(LifetimeMode.ADOPTABLE).spawn(payload)\n"
        "open(sys.argv[1],'w').write(json.dumps(managed.token.to_dict()))\n",
        encoding="utf-8",
    )
    __import__("subprocess").run(
        [sys.executable, str(launcher), str(token_file), str(marker)],
        check=True,
        timeout=10,
    )
    token = SupervisionToken.from_dict(json.loads(token_file.read_text(encoding="utf-8")))
    while not marker.exists():
        time.sleep(0.05)
    assert identity_matches(token.identity)
    adopted = adopt(token)
    with pytest.raises(ValueError, match="already adopted"):
        adopt(token)
    assert terminate(adopted, grace_seconds=0.1)
    deadline = time.monotonic() + 10
    while identity_matches(adopted.identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not identity_matches(adopted.identity)


@pytest.mark.skipif(os.name != "nt", reason="native Windows detached launcher integration")
@pytest.mark.parametrize("workflow", ["update-refresh", "background-web-service"])
def test_windows_detached_workflows_survive_launcher_and_stop_by_identity(tmp_path: Path, workflow: str) -> None:
    """Model both short-lived call sites with a real launcher and payload."""
    token_file = tmp_path / f"{workflow}.json"
    marker = tmp_path / f"{workflow}.alive"
    launcher = tmp_path / f"launch-{workflow}.py"
    launcher.write_text(
        "import json,sys\n"
        "from spec_runtime.process_supervisor import LifetimeMode,ProcessSupervisor\n"
        "payload=[sys.executable,'-c',"
        "'from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text(\"alive\"); time.sleep(30)',sys.argv[2]]\n"
        "managed=ProcessSupervisor(LifetimeMode.DETACHED).spawn(payload)\n"
        "open(sys.argv[1],'w').write(json.dumps(managed.token.to_dict()))\n",
        encoding="utf-8",
    )
    launcher_process = __import__("subprocess").run(
        [sys.executable, str(launcher), str(token_file), str(marker)],
        check=True,
        timeout=10,
    )
    assert launcher_process.returncode == 0
    token = SupervisionToken.from_dict(json.loads(token_file.read_text(encoding="utf-8")))
    while not marker.exists():
        time.sleep(0.05)
    assert identity_matches(token.identity)
    assert terminate(token, grace_seconds=0.1)
    deadline = time.monotonic() + 10
    while identity_matches(token.identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not identity_matches(token.identity)


@pytest.mark.skipif(os.name != "nt", reason="native Windows background web integration")
def test_windows_background_web_server_survives_launcher_and_stops_by_token(tmp_path: Path) -> None:
    """Exercise the real web start/status/stop lifecycle, including its persisted token."""
    pytest.importorskip("uvicorn")
    config_path = tmp_path / ".spec.toml"
    config_path.write_text('base_ref = "origin/main"\n', encoding="utf-8")
    launcher_env = os.environ.copy()
    launcher_env["SPEC_CONFIG"] = str(config_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    launch = (
        "import sys; from pathlib import Path; "
        "from spec_runtime.web.server import run_server; "
        "raise SystemExit(run_server(Path(sys.argv[1]),port=int(sys.argv[2]),background=True))"
    )
    subprocess.run(
        [sys.executable, "-c", launch, str(tmp_path), str(port)],
        check=True,
        timeout=20,
        env=launcher_env,
    )
    token_path = tmp_path / ".spec-state" / "web" / "server.supervision.json"
    token = SupervisionToken.from_dict(json.loads(token_path.read_text(encoding="utf-8")))
    try:
        assert identity_matches(token.identity)
        from spec_runtime.web.server import is_server_running, stop_server

        assert is_server_running(tmp_path) == (True, token.payload.pid)
        assert stop_server(tmp_path) == 0
        deadline = time.monotonic() + 10
        while identity_matches(token.identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not identity_matches(token.identity)
        assert not token_path.exists()
    finally:
        if identity_matches(token.identity):
            terminate(token, grace_seconds=0.1)
