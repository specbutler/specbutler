from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runtime import container
from spec_runtime.config import (
    AgentConfig,
    AutopilotConfig,
    BootstrapCacheConfig,
    ContainerExecutionConfig,
    ExecutionConfig,
    SpecRuntimeConfig,
)
from spec_runtime.control_plane import save_run_lease
from spec_runtime.control_plane.lease import build_lease
from spec_runtime.execution_backend import (
    CommandRequest,
    WorkspaceHandle,
    host_spec_runtime_source_id,
    host_spec_runtime_version,
)


class FakeRunner:
    def __init__(self, *, permission_failure: bool = False, engine: str = "docker"):
        self.calls: list[list[str]] = []
        self.permission_failure = permission_failure
        self.engine = engine

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, input_text, timeout
        self.calls.append(argv)
        if argv == [self.engine, "--version"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.engine} version 1\n", "")
        if argv == [self.engine, "info"] and self.permission_failure:
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "permission denied while trying to connect to the Docker daemon socket",
            )
        if argv == [self.engine, "info"]:
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")
        if argv[:2] == [self.engine, "run"]:
            return subprocess.CompletedProcess(argv, 0, "hello\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeBackend:
    def __init__(self, *, fail_cleanup: bool = False):
        self.identity = type("Identity", (), {"backend": "container"})()
        self.commands: list[CommandRequest] = []
        self.cleaned = False
        self.cleanup_allow_unpushed_work = False
        self.fail_cleanup = fail_cleanup
        self.workspace = WorkspaceHandle(
            path=Path("/tmp/spec-smoke/source"),
            outbox_path=Path("/tmp/spec-smoke/outbox"),
            branch="main",
            backend="container",
            metadata={"logs_path": "/tmp/spec-smoke/logs"},
        )

    def prepare_workspace(self, **kwargs) -> WorkspaceHandle:  # noqa: ANN003
        self.prepare_kwargs = kwargs
        return self.workspace

    def run_command(self, request: CommandRequest):
        self.commands.append(request)
        if request.argv == ["spec", "--version"]:
            output = host_spec_runtime_version()
        elif request.argv == ["spec", "--source-id"]:
            output = host_spec_runtime_source_id()
        else:
            output = "ok"
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": f"{output}\n", "stderr": "", "argv": request.argv},
        )()

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        assert workspace is self.workspace
        self.cleaned = True
        self.cleanup_allow_unpushed_work = allow_unpushed_work
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")


class FakeGcRunner:
    def __init__(self, inventory: dict[str, list[dict[str, object]]]):
        self.inventory = inventory
        self.calls: list[list[str]] = []
        self.fail_inventory_kind = ""

    def run(self, argv: list[str], *, cwd: Path, **kwargs):  # noqa: ANN003
        del cwd, kwargs
        self.calls.append(argv)
        if argv[1] == "ps" or (len(argv) > 2 and argv[2] == "ls"):
            kind = "container" if argv[1] == "ps" else argv[1]
            if kind == self.fail_inventory_kind:
                return subprocess.CompletedProcess(argv, 1, "", "inventory failed")
            references = []
            for item in self.inventory.get(kind, []):
                raw_labels = (
                    (item.get("Config") or item.get("config") or {}).get(
                        "Labels",
                        (item.get("Config") or item.get("config") or {}).get(
                            "labels", {}
                        ),
                    )
                    if kind == "container"
                    and isinstance(item.get("Config") or item.get("config"), dict)
                    else item.get("Labels") or item.get("labels") or {}
                )
                labels = raw_labels if isinstance(raw_labels, dict) else {}
                if (
                    "label=spec.owner=spec-runtime" in argv
                    and labels.get("spec.owner") != "spec-runtime"
                ):
                    continue
                references.append(
                    str(
                        item.get("Id")
                        or item.get("ID")
                        or item.get("id")
                        or item.get("Name")
                        or item.get("name")
                        or ""
                    )
                )
            output = "\n".join(reference for reference in references if reference)
            return subprocess.CompletedProcess(argv, 0, output, "")
        if argv[1] == "inspect":
            kind = "container"
        elif len(argv) > 2 and argv[2] == "inspect":
            kind = argv[1]
        else:
            kind = ""
        if kind:
            reference = argv[-1]
            for item in self.inventory.get(kind, []):
                if reference in {
                    str(item.get("Id") or item.get("ID") or item.get("id") or ""),
                    str(item.get("Name") or item.get("name") or ""),
                }:
                    return subprocess.CompletedProcess(argv, 0, json.dumps([item]), "")
            return subprocess.CompletedProcess(argv, 1, "", "not found")
        if argv[1:3] == ["rm", "-f"]:
            kind = "container"
            reference = argv[-1]
        elif len(argv) > 3 and argv[2] == "rm":
            kind = argv[1]
            reference = argv[-1]
        else:
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.inventory[kind] = [
            item
            for item in self.inventory.get(kind, [])
            if reference not in {str(item.get("Id") or ""), str(item.get("Name") or "")}
        ]
        return subprocess.CompletedProcess(argv, 0, "", "")


def _config(**overrides) -> SpecRuntimeConfig:
    execution = overrides.pop(
        "execution",
        ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(image="example/spec-worker:latest"),
        ),
    )
    return replace(SpecRuntimeConfig(), execution=execution, **overrides)


def _gc_labels(
    repo: Path,
    run_id: str,
    *,
    spec_id: str,
    explicit_scope: bool = False,
    workspace_scope: Path | None = None,
) -> dict[str, str]:
    scope = (workspace_scope or (repo / ".spec-workspaces")).resolve()
    labels = {
        "spec.owner": "spec-runtime",
        "spec.run_id": run_id,
        "spec.spec_id": spec_id,
        "spec.phase": "execution",
        "spec.workspace_root": str(scope / run_id / "source"),
    }
    if explicit_scope:
        labels["spec.workspace_scope"] = str(scope)
    return labels


def _gc_container(
    resource_id: str,
    name: str,
    state: str,
    labels: dict[str, str],
) -> dict[str, object]:
    return {
        "Id": resource_id,
        "Name": f"/{name}",
        "Config": {"Labels": labels},
        "State": {"Status": state},
    }


def _gc_volume(name: str, labels: dict[str, str]) -> dict[str, object]:
    return {"Name": name, "Labels": labels}


def _gc_network(
    resource_id: str,
    name: str,
    labels: dict[str, str],
) -> dict[str, object]:
    return {"Id": resource_id, "Name": name, "Labels": labels}


def _init_gc_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_container_gc_protects_active_and_unrelated_resources(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    (runs / "active.json").write_text(json.dumps({"run_id": "active-run", "status": "running", "phase": "implement"}))
    active = tmp_path / ".spec-state" / "autopilot"
    active.mkdir()
    (active / "active.json").write_text(json.dumps({"active": {"run_id": "active-run"}}))
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "active",
                    "active",
                    "exited",
                    _gc_labels(tmp_path, "active-run", spec_id="active"),
                ),
                _gc_container(
                    "stale",
                    "stale",
                    "exited",
                    _gc_labels(tmp_path, "done-run", spec_id="done"),
                ),
                _gc_container("other", "database", "exited", {"team": "product"}),
                _gc_container(
                    "legacy",
                    "spec-0123456789abcdef-worker",
                    "exited",
                    {},
                ),
            ],
            "volume": [_gc_volume("spec-0123456789abcdef-source", {})],
            "network": [_gc_network("net", "unrelated", {})],
        }
    )

    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)

    assert [(item.kind, item.name) for item in resources] == [("container", "stale")]


def test_container_gc_apply_removes_only_discovered_stale_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_gc_repo(tmp_path)
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "stale-id",
                    "stale-worker",
                    "exited",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                ),
                _gc_container("other-id", "product-db", "exited", {"team": "product"}),
                _gc_container(
                    "foreign-container",
                    "foreign-worker",
                    "exited",
                    _gc_labels(
                        tmp_path.parent / "foreign-repo",
                        "feature-finished",
                        spec_id="feature",
                    ),
                ),
            ],
            "volume": [
                _gc_volume(
                    "local-volume",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                ),
                _gc_volume(
                    "foreign-volume",
                    _gc_labels(
                        tmp_path.parent / "foreign-repo",
                        "feature-finished",
                        spec_id="feature",
                    ),
                ),
            ],
            "network": [
                _gc_network(
                    "local-network-id",
                    "local-network",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                ),
                _gc_network(
                    "foreign-network-id",
                    "foreign-network",
                    _gc_labels(
                        tmp_path.parent / "foreign-repo",
                        "feature-finished",
                        spec_id="feature",
                    ),
                ),
            ],
        }
    )
    monkeypatch.setattr(container, "_SubprocessContainerRunner", lambda: runner)

    result = container.cmd_gc(argparse.Namespace(repo_root=str(tmp_path), apply=True))

    assert result == 0
    assert ["docker", "rm", "-f", "stale-id"] in runner.calls
    assert ["docker", "volume", "rm", "local-volume"] in runner.calls
    assert not any(call[:4] == ["docker", "volume", "rm", "-f"] for call in runner.calls)
    assert ["docker", "network", "rm", "local-network-id"] in runner.calls
    assert not any("other-id" in call for call in runner.calls)
    assert all(
        resource
        not in " ".join(
            " ".join(call)
            for call in runner.calls
            if "rm" in call
        )
        for resource in (
            "foreign-container",
            "foreign-volume",
            "foreign-network-id",
        )
    )


def test_container_gc_dry_run_lists_scoped_resources_and_ignores_legacy_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_gc_repo(tmp_path)
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "stale-id",
                    "stale-worker",
                    "exited",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                ),
                _gc_container(
                    "legacy-id",
                    "spec-0123456789abcdef-worker",
                    "created",
                    {},
                ),
            ],
            "network": [
                _gc_network(
                    "network-id",
                    "stale-network",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                )
            ],
        }
    )
    monkeypatch.setattr(container, "_SubprocessContainerRunner", lambda: runner)

    result = container.cmd_gc(argparse.Namespace(repo_root=str(tmp_path), apply=False))

    assert result == 0
    assert capsys.readouterr().out.splitlines() == [
        "would remove container stale-worker: container is stopped",
        "would remove network stale-network: owning run is finished or missing",
        "Re-run with --apply to remove these resources.",
    ]
    assert not any(call[1:3] in (["rm", "-f"], ["volume", "rm"], ["network", "rm"]) for call in runner.calls)


def test_container_gc_discovers_running_worker_despite_truncated_command(tmp_path: Path) -> None:
    # Docker truncates the Command column with a Unicode ellipsis, so the literal
    # "sleep infinity" is never present. Detection must rely on labels instead.
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    (runs / "feature-finished.json").write_text(
        json.dumps(
            {
                "run_id": "feature-finished",
                "spec_id": "feature",
                "status": "passed",
                "phase": "cleanup",
            }
        )
    )
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "worker-id",
                    "spec-worker",
                    "running",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                ),
            ]
        }
    )

    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)

    assert [(item.kind, item.name, item.reason) for item in resources] == [
        ("container", "spec-worker", "running worker of finished run"),
    ]


def test_container_gc_preserves_transitional_passed_run_with_live_lease(
    tmp_path: Path,
) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-live"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                # Phase runners persist this transiently between phases; it is
                # not terminal while the workflow still owns a live lease.
                "status": "passed",
                "phase": "implement",
            }
        )
    )
    save_run_lease(
        runs,
        build_lease(
            run_id=run_id,
            spec_id="feature",
            phase="implement",
            timeout_seconds=600,
        ),
    )
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "worker-id",
                    "spec-worker",
                    "running",
                    _gc_labels(tmp_path, run_id, spec_id="feature"),
                )
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


def test_container_gc_protects_run_when_lease_changes_during_projection(
    tmp_path: Path,
) -> None:
    from spec_runtime import spec_status

    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-lease-race"
    run_payload = {
        "run_id": run_id,
        "spec_id": "feature",
        "status": "passed",
        "phase": "implement",
    }
    (runs / f"{run_id}.json").write_text(json.dumps(run_payload))
    expired = replace(
        build_lease(
            run_id=run_id,
            spec_id="feature",
            phase="implement",
            timeout_seconds=1,
        ),
        heartbeat_at="2000-01-01T00:00:00+00:00",
    )
    save_run_lease(runs, expired)
    original_project = spec_status.project_run_record_status

    def replace_lease_after_projection(state_runs_dir, run):
        projected = original_project(state_runs_dir, run)
        save_run_lease(
            runs,
            build_lease(
                run_id=run_id,
                spec_id="feature",
                phase="implement",
                timeout_seconds=600,
            ),
        )
        return projected

    with patch.object(
        spec_status,
        "project_run_record_status",
        side_effect=replace_lease_after_projection,
    ):
        knowledge = container._gc_run_knowledge(tmp_path)

    assert run_id in knowledge.active
    assert run_id not in knowledge.terminal


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_container_gc_lease_fifo_is_bounded_and_protects_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-fifo-lease"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                "status": "passed",
                "phase": "cleanup",
            }
        )
    )
    run_state_dir = runs / run_id
    run_state_dir.mkdir()
    os.mkfifo(run_state_dir / "lease.json")
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from spec_runtime.container import _gc_run_knowledge",
            "knowledge = _gc_run_knowledge(Path(sys.argv[1]))",
            f"assert {run_id!r} in knowledge.active",
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_container_gc_oversized_lease_protects_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-oversized-lease"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                "status": "passed",
                "phase": "cleanup",
            }
        )
    )
    run_state_dir = runs / run_id
    run_state_dir.mkdir()
    with (run_state_dir / "lease.json").open("wb") as stream:
        stream.truncate(container._GC_SMALL_STATE_MAX_BYTES + 1)

    knowledge = container._gc_run_knowledge(tmp_path)

    assert run_id in knowledge.active
    assert run_id not in knowledge.terminal


@pytest.mark.parametrize(
    "raw",
    [
        '{"number":' + "9" * 5000 + "}",
        "[" * 10000 + "]" * 10000,
    ],
)
def test_container_gc_pathological_bounded_run_json_protects_filename(
    tmp_path: Path,
    raw: str,
) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-pathological-json"
    (runs / f"{run_id}.json").write_text(raw)

    knowledge = container._gc_run_knowledge(tmp_path)

    assert run_id in knowledge.active
    assert run_id not in knowledge.terminal


@pytest.mark.parametrize(
    ("leaf", "payload"),
    [
        ("lease.json", "not-json"),
        ("lease.json", json.dumps({"run_id": "wrong", "spec_id": "feature"})),
        ("gate-records.json", json.dumps({"version": 1, "records": "wrong"})),
    ],
)
def test_container_gc_malformed_projection_state_protects_passed_run(
    tmp_path: Path,
    leaf: str,
    payload: str,
) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-malformed-projection"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                "status": "passed",
                "phase": "cleanup",
            }
        )
    )
    run_state_dir = runs / run_id
    run_state_dir.mkdir()
    (run_state_dir / leaf).write_text(payload)

    knowledge = container._gc_run_knowledge(tmp_path)

    assert run_id in knowledge.active
    assert run_id not in knowledge.terminal


def test_container_gc_incomplete_lease_protects_passed_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-incomplete-lease"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                "status": "passed",
                "phase": "cleanup",
            }
        )
    )
    run_state_dir = runs / run_id
    run_state_dir.mkdir()
    (run_state_dir / "lease.json").write_text(
        json.dumps({"run_id": run_id, "spec_id": "feature"})
    )

    knowledge = container._gc_run_knowledge(tmp_path)

    assert run_id in knowledge.active
    assert run_id not in knowledge.terminal


def test_container_gc_huge_finite_lease_timeout_protects_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-huge-timeout"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                "status": "passed",
                "phase": "cleanup",
            }
        )
    )
    save_run_lease(
        runs,
        build_lease(
            run_id=run_id,
            spec_id="feature",
            phase="cleanup",
            timeout_seconds=1e308,
        ),
    )

    knowledge = container._gc_run_knowledge(tmp_path)

    assert run_id in knowledge.active
    assert run_id not in knowledge.terminal


def test_container_gc_ignores_other_checkout_and_ambiguous_resources(
    tmp_path: Path,
) -> None:
    other_repo = tmp_path.parent / "other-repo"
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "local-new",
                    "local-new",
                    "exited",
                    _gc_labels(tmp_path, "local-new", spec_id="local"),
                ),
                _gc_container(
                    "local-0.4.0",
                    "local-0.4.0",
                    "exited",
                    _gc_labels(tmp_path, "local-old", spec_id="local"),
                ),
                _gc_container(
                    "foreign-new",
                    "foreign-new",
                    "running",
                    _gc_labels(other_repo, "foreign-new", spec_id="foreign"),
                ),
                _gc_container(
                    "foreign-0.4.0",
                    "foreign-0.4.0",
                    "exited",
                    _gc_labels(other_repo, "foreign-old", spec_id="foreign"),
                ),
                _gc_container(
                    "unscoped",
                    "unscoped",
                    "exited",
                    {
                        "spec.owner": "spec-runtime",
                        "spec.run_id": "unknown-run",
                        "spec.spec_id": "unknown",
                    },
                ),
                _gc_container(
                    "legacy",
                    "spec-0123456789abcdef-worker",
                    "exited",
                    {},
                ),
            ]
        }
    )

    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)

    assert [(item.kind, item.name) for item in resources] == [
        ("container", "local-0.4.0"),
        ("container", "local-new"),
    ]


def test_container_gc_explicit_scope_mismatch_fails_closed(tmp_path: Path) -> None:
    labels = _gc_labels(tmp_path, "feature-finished", spec_id="feature")
    labels["spec.workspace_scope"] = str((tmp_path.parent / "other").resolve())
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container("mismatch", "mismatch", "exited", labels)
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda labels, scope: labels.pop("spec.run_id"),
        lambda labels, scope: labels.pop("spec.spec_id"),
        lambda labels, scope: labels.pop("spec.workspace_root"),
        lambda labels, scope: labels.__setitem__("spec.run_id", "other-run"),
        lambda labels, scope: labels.__setitem__("spec.spec_id", "other"),
        lambda labels, scope: labels.__setitem__(
            "spec.workspace_root",
            str(scope / "nested" / labels["spec.run_id"] / "source"),
        ),
        lambda labels, scope: labels.__setitem__(
            "spec.workspace_root",
            str(scope / labels["spec.run_id"] / ".." / labels["spec.run_id"] / "source"),
        ),
    ],
)
def test_container_gc_rejects_incomplete_or_noncanonical_identity(
    tmp_path: Path,
    mutation,
) -> None:  # noqa: ANN001
    labels = _gc_labels(tmp_path, "feature-finished", spec_id="feature")
    mutation(labels, (tmp_path / ".spec-workspaces").resolve())
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container("candidate", "candidate", "exited", labels)
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


def test_container_gc_structured_labels_support_repo_path_with_comma(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo,with-comma"
    repo.mkdir()
    labels = _gc_labels(repo, "feature-finished", spec_id="feature")
    runner = FakeGcRunner(
        {
            "volume": [_gc_volume("comma-volume", labels)],
        }
    )

    resources = container.discover_gc_resources(repo, "docker", runner=runner)

    assert [(resource.kind, resource.name) for resource in resources] == [
        ("volume", "comma-volume")
    ]


def test_container_gc_protects_running_worker_without_terminal_state(
    tmp_path: Path,
) -> None:
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "running-id",
                    "running-worker",
                    "running",
                    _gc_labels(tmp_path, "feature-missing", spec_id="feature"),
                )
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


def test_container_gc_corrupt_run_state_protects_matching_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    (runs / "feature-corrupt.json").write_text("not-json")
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "stopped-id",
                    "stopped-worker",
                    "exited",
                    _gc_labels(tmp_path, "feature-corrupt", spec_id="feature"),
                )
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


def test_container_gc_oversized_run_state_protects_matching_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    with (runs / "feature-oversized.json").open("wb") as stream:
        stream.truncate(container._GC_STATE_MAX_BYTES + 1)
    runner = FakeGcRunner(
        {
            "volume": [
                _gc_volume(
                    "oversized-volume",
                    _gc_labels(tmp_path, "feature-oversized", spec_id="feature"),
                )
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliably available")
def test_container_gc_linked_run_state_protects_matching_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    target = tmp_path / "outside-run.json"
    target.write_text(
        json.dumps(
            {
                "run_id": "feature-linked",
                "spec_id": "feature",
                "status": "passed",
            }
        )
    )
    (runs / "feature-linked.json").symlink_to(target)
    runner = FakeGcRunner(
        {
            "volume": [
                _gc_volume(
                    "linked-volume",
                    _gc_labels(tmp_path, "feature-linked", spec_id="feature"),
                )
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_container_gc_run_state_fifo_is_bounded_and_protects_run(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    os.mkfifo(runs / "feature-fifo.json")
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from spec_runtime.container import _gc_run_knowledge",
            "knowledge = _gc_run_knowledge(Path(sys.argv[1]))",
            "assert 'feature-fifo' in knowledge.active",
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_container_gc_mismatched_run_state_identity_protects_both_runs(
    tmp_path: Path,
) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    (runs / "feature-first.json").write_text(
        json.dumps(
            {
                "run_id": "feature-second",
                "spec_id": "feature",
                "status": "running",
                "phase": "implement",
            }
        )
    )
    runner = FakeGcRunner(
        {
            "volume": [
                _gc_volume(
                    "first-volume",
                    _gc_labels(tmp_path, "feature-first", spec_id="feature"),
                ),
                _gc_volume(
                    "second-volume",
                    _gc_labels(tmp_path, "feature-second", spec_id="feature"),
                ),
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


def test_container_gc_unknown_run_status_is_conservatively_active(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    run_id = "feature-future"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "feature",
                "status": "future-status",
            }
        )
    )
    runner = FakeGcRunner(
        {
            "volume": [
                _gc_volume(
                    "future-volume",
                    _gc_labels(tmp_path, run_id, spec_id="feature"),
                )
            ]
        }
    )

    assert container.discover_gc_resources(tmp_path, "docker", runner=runner) == []


@pytest.mark.skipif(os.name == "nt", reason="requires directory symlinks")
def test_container_gc_refuses_linked_run_state_directory(tmp_path: Path) -> None:
    state_root = tmp_path / ".spec-state"
    state_root.mkdir()
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    (state_root / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(container.ContainerGcError, match="linked state boundary"):
        container.discover_gc_resources(
            tmp_path,
            "docker",
            runner=FakeGcRunner({}),
        )


def test_container_gc_refuses_corrupt_autopilot_state(tmp_path: Path) -> None:
    autopilot = tmp_path / ".spec-state" / "autopilot"
    autopilot.mkdir(parents=True)
    (autopilot / "active.json").write_text("not-json")

    with pytest.raises(container.ContainerGcError, match="unreadable autopilot"):
        container.discover_gc_resources(
            tmp_path,
            "docker",
            runner=FakeGcRunner({}),
        )


def test_container_gc_refuses_oversized_autopilot_state(tmp_path: Path) -> None:
    autopilot = tmp_path / ".spec-state" / "autopilot"
    autopilot.mkdir(parents=True)
    with (autopilot / "active.json").open("wb") as stream:
        stream.truncate(container._GC_STATE_MAX_BYTES + 1)

    with pytest.raises(container.ContainerGcError, match="unreadable autopilot"):
        container.discover_gc_resources(
            tmp_path,
            "docker",
            runner=FakeGcRunner({}),
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliably available")
def test_container_gc_refuses_linked_autopilot_state(tmp_path: Path) -> None:
    autopilot = tmp_path / ".spec-state" / "autopilot"
    autopilot.mkdir(parents=True)
    target = tmp_path / "outside-autopilot.json"
    target.write_text("{}")
    (autopilot / "active.json").symlink_to(target)

    with pytest.raises(container.ContainerGcError, match="unreadable autopilot"):
        container.discover_gc_resources(
            tmp_path,
            "docker",
            runner=FakeGcRunner({}),
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_container_gc_autopilot_fifo_fails_closed_without_blocking(tmp_path: Path) -> None:
    autopilot = tmp_path / ".spec-state" / "autopilot"
    autopilot.mkdir(parents=True)
    os.mkfifo(autopilot / "active.json")
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from spec_runtime.container import ContainerGcError, _gc_run_knowledge",
            "try:",
            "    _gc_run_knowledge(Path(sys.argv[1]))",
            "except ContainerGcError:",
            "    raise SystemExit(0)",
            "raise SystemExit('GC unexpectedly accepted autopilot FIFO')",
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_container_gc_refuses_mismatched_autopilot_identity(tmp_path: Path) -> None:
    autopilot = tmp_path / ".spec-state" / "autopilot"
    autopilot.mkdir(parents=True)
    (autopilot / "active.json").write_text(
        json.dumps({"first": {"run_id": "second-run"}})
    )

    with pytest.raises(container.ContainerGcError, match="malformed autopilot"):
        container.discover_gc_resources(
            tmp_path,
            "docker",
            runner=FakeGcRunner({}),
        )


def test_container_gc_inventory_failure_is_not_partial_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_gc_repo(tmp_path)
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "stale-id",
                    "stale-worker",
                    "exited",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                )
            ]
        }
    )
    runner.fail_inventory_kind = "volume"
    monkeypatch.setattr(container, "_SubprocessContainerRunner", lambda: runner)

    assert container.cmd_gc(argparse.Namespace(repo_root=str(tmp_path), apply=True)) == 1
    assert "inventory failed" in capsys.readouterr().err
    assert not any("rm" in call for call in runner.calls)


def test_container_gc_apply_revalidates_labels_and_active_state(tmp_path: Path) -> None:
    labels = _gc_labels(tmp_path, "feature-finished", spec_id="feature")
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container("stale-id", "stale-worker", "exited", labels)
            ]
        }
    )
    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)
    labels["spec.workspace_root"] = str(
        tmp_path.parent / "foreign" / ".spec-workspaces" / "feature-finished" / "source"
    )

    with pytest.raises(container.ContainerGcError, match="changed before apply"):
        container._apply_gc_resources(
            tmp_path,
            "docker",
            resources,
            runner=runner,
            state_dir=".spec-state",
            workspace_scope=(tmp_path / ".spec-workspaces").resolve(),
        )

    assert not any(call[1:3] == ["rm", "-f"] for call in runner.calls)


def test_container_gc_apply_refuses_run_that_became_active(tmp_path: Path) -> None:
    runner = FakeGcRunner(
        {
            "container": [
                _gc_container(
                    "stale-id",
                    "stale-worker",
                    "exited",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                )
            ]
        }
    )
    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    (runs / "feature-finished.json").write_text(
        json.dumps(
            {
                "run_id": "feature-finished",
                "spec_id": "feature",
                "status": "running",
                "phase": "implement",
            }
        )
    )

    with pytest.raises(container.ContainerGcError, match="changed before apply"):
        container._apply_gc_resources(
            tmp_path,
            "docker",
            resources,
            runner=runner,
            state_dir=".spec-state",
            workspace_scope=(tmp_path / ".spec-workspaces").resolve(),
        )

    assert not any(call[1:3] == ["rm", "-f"] for call in runner.calls)


def test_container_gc_apply_rechecks_liveness_immediately_before_removal(
    tmp_path: Path,
) -> None:
    labels = _gc_labels(tmp_path, "feature-finished", spec_id="feature")
    runs = tmp_path / ".spec-state" / "runs"

    class LivenessRaceRunner(FakeGcRunner):
        inspect_count = 0

        def run(self, argv: list[str], *, cwd: Path, **kwargs):  # noqa: ANN003
            result = super().run(argv, cwd=cwd, **kwargs)
            if argv[1] == "inspect":
                self.inspect_count += 1
                if self.inspect_count == 3:
                    runs.mkdir(parents=True)
                    (runs / "feature-finished.json").write_text(
                        json.dumps(
                            {
                                "run_id": "feature-finished",
                                "spec_id": "feature",
                                "status": "running",
                                "phase": "implement",
                            }
                        )
                    )
            return result

    runner = LivenessRaceRunner(
        {
            "container": [
                _gc_container("stale-id", "stale-worker", "exited", labels)
            ]
        }
    )
    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)

    with pytest.raises(container.ContainerGcError, match="changed during apply"):
        container._apply_gc_resources(
            tmp_path,
            "docker",
            resources,
            runner=runner,
            state_dir=".spec-state",
            workspace_scope=(tmp_path / ".spec-workspaces").resolve(),
        )

    assert not any(call[1:3] == ["rm", "-f"] for call in runner.calls)


@pytest.mark.parametrize("workspace", [".", "../outside"])
def test_container_gc_refuses_unsafe_workspace_scope(
    tmp_path: Path,
    workspace: str,
) -> None:
    with pytest.raises(container.ContainerGcError, match="workspace root"):
        container._resolved_workspace_scope(tmp_path, workspace)


def test_container_gc_refuses_symlinked_workspace_scope(tmp_path: Path) -> None:
    target = tmp_path / "actual-workspaces"
    target.mkdir()
    (tmp_path / "linked-workspaces").symlink_to(target, target_is_directory=True)

    with pytest.raises(container.ContainerGcError, match="symlink/junction"):
        container._resolved_workspace_scope(tmp_path, "linked-workspaces")


def test_container_gc_uses_common_root_from_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_gc_repo(tmp_path)
    subdirectory = tmp_path / "nested" / "directory"
    subdirectory.mkdir(parents=True)
    runner = FakeGcRunner(
        {
            "volume": [
                _gc_volume(
                    "local-volume",
                    _gc_labels(tmp_path, "feature-finished", spec_id="feature"),
                )
            ]
        }
    )
    monkeypatch.setattr(container, "_SubprocessContainerRunner", lambda: runner)

    assert (
        container.cmd_gc(
            argparse.Namespace(repo_root=str(subdirectory), apply=False)
        )
        == 0
    )
    assert any(call[-1] == "local-volume" for call in runner.calls if "inspect" in call)


def test_container_gc_supports_configured_absolute_workspace_root(
    tmp_path: Path,
) -> None:
    workspace_scope = tmp_path / "custom-workspaces"
    labels = _gc_labels(
        tmp_path,
        "feature-finished",
        spec_id="feature",
        workspace_scope=workspace_scope,
    )
    runner = FakeGcRunner(
        {"volume": [_gc_volume("custom-volume", labels)]}
    )

    resources = container.discover_gc_resources(
        tmp_path,
        "docker",
        runner=runner,
        workspace_root=str(workspace_scope),
    )

    assert [resource.name for resource in resources] == ["custom-volume"]


def test_container_gc_accepts_podman_lowercase_network_inspection(
    tmp_path: Path,
) -> None:
    labels = _gc_labels(tmp_path, "feature-finished", spec_id="feature")
    runner = FakeGcRunner(
        {
            "network": [
                {
                    "id": "podman-network-id",
                    "name": "podman-network",
                    "labels": labels,
                }
            ]
        }
    )

    resources = container.discover_gc_resources(
        tmp_path,
        "podman",
        runner=runner,
    )

    assert [(resource.resource_id, resource.name) for resource in resources] == [
        ("podman-network-id", "podman-network")
    ]


def test_doctor_reports_healthy_docker(tmp_path: Path) -> None:
    config = _config()
    runner = FakeRunner()

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=runner,
            system_name="Darwin",
        )

    assert all(check.ok for check in checks)
    assert ["docker", "info"] in runner.calls
    assert any(check.name == "worker image source" and check.detail.startswith("image:") for check in checks)


def test_doctor_reports_healthy_podman(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(engine="podman", image="worker:latest"),
        )
    )
    runner = FakeRunner(engine="podman")

    with patch("shutil.which", return_value="/usr/bin/podman"):
        checks = container.run_doctor_checks(tmp_path, config, runner=runner, system_name="Linux")

    assert all(check.ok for check in checks)
    assert ["podman", "run", "--rm", "hello-world"] in runner.calls


def test_doctor_missing_engine_binary_is_actionable(tmp_path: Path) -> None:
    config = _config()

    with patch("shutil.which", return_value=None):
        checks = container.run_doctor_checks(tmp_path, config, runner=FakeRunner())

    engine = checks[0]
    assert engine.name == "engine binary"
    assert engine.ok is False
    assert "not found" in engine.detail
    assert any(".spec.local.toml" in line for line in engine.remediation)


def test_doctor_docker_permission_failure_prints_ubuntu_next_steps(tmp_path: Path) -> None:
    config = _config()

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(permission_failure=True),
            system_name="Linux",
        )

    api = next(check for check in checks if check.name == "daemon/API")
    assert api.ok is False
    assert any("usermod -aG docker" in line for line in api.remediation)
    assert any("root-equivalent" in line for line in api.remediation)


def test_doctor_reports_missing_worker_dockerfile(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(image="", dockerfile=".spec/worker.Dockerfile"),
        )
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
        )

    source = next(check for check in checks if check.name == "worker image source")
    assert source.ok is False
    assert source.detail == "missing-dockerfile:.spec/worker.Dockerfile"
    assert source.remediation == ("Run: spec container init",)


def test_doctor_warns_when_build_ssh_set_without_agent(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(
                image="example/spec-worker:latest",
                build_ssh="default",
            ),
        )
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={},
        )

    ssh_check = next(check for check in checks if check.name == "build SSH agent")
    assert ssh_check.ok is False
    assert ssh_check.detail == "SSH_AUTH_SOCK unset"
    assert any('eval "$(ssh-agent -s)"' in line for line in ssh_check.remediation)
    assert any("build_ssh" in line for line in ssh_check.remediation)


def test_doctor_passes_when_build_ssh_set_with_agent(tmp_path: Path) -> None:
    sock_path = tmp_path / "agent.sock"
    sock_path.touch()
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(
                image="example/spec-worker:latest",
                build_ssh="default",
            ),
        )
    )

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.object(container.stat, "S_ISSOCK", return_value=True),
    ):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={"SSH_AUTH_SOCK": str(sock_path)},
        )

    ssh_check = next(check for check in checks if check.name == "build SSH agent")
    assert ssh_check.ok is True
    assert str(sock_path) in ssh_check.detail


def test_doctor_omits_build_ssh_check_when_unset(tmp_path: Path) -> None:
    config = _config()

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={},
        )

    assert not any(check.name == "build SSH agent" for check in checks)


def test_doctor_warns_when_build_ssh_socket_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sock"
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(
                image="example/spec-worker:latest",
                build_ssh="default",
            ),
        )
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={"SSH_AUTH_SOCK": str(missing)},
        )

    ssh_check = next(check for check in checks if check.name == "build SSH agent")
    assert ssh_check.ok is False
    assert "not found" in ssh_check.detail


def test_container_init_creates_files_and_preserves_existing_without_force(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="https://github.com/acme/spec.git",
    )

    versions = {"claude": "2.1.233", "codex": "0.147.0"}
    with patch(
        "spec_runtime.container._detect_agent_cli_version",
        side_effect=lambda agent: versions.get(agent, ""),
    ):
        assert container.cmd_init(args) == 0
    dockerfile = tmp_path / ".spec" / "worker.Dockerfile"
    dockerfile_text = dockerfile.read_text()
    assert "FROM python:3.12-bookworm" in dockerfile_text
    assert "nodejs" in dockerfile_text
    assert "npm" in dockerfile_text
    assert (
        '"specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"'
        in dockerfile_text
    )
    assert "ARG SPEC_BUTLER_REPOSITORY_URL=https://github.com/acme/spec.git" in dockerfile_text
    assert "@anthropic-ai/claude-code@2.1.233" in dockerfile_text
    assert "@openai/codex@0.147.0" in dockerfile_text
    assert 'sh -lc "$SPEC_BOOTSTRAP_INSTALL_COMMAND"' not in dockerfile_text
    assert not (tmp_path / ".spec" / ".dockerignore").exists()

    dockerfile.write_text("custom\n")
    assert container.cmd_init(args) == 0
    assert dockerfile.read_text() == "custom\n"


def test_container_init_installs_only_configured_agents(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[agents]
allowed = ["codex"]

[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False)

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "RUN npm install -g @openai/codex" in dockerfile_text
    assert "@anthropic-ai/claude-code" not in dockerfile_text


def test_container_init_reserves_build_ssh_for_project_dependencies(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
build_ssh = "default"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False)

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "python -m pip install --no-cache-dir" in dockerfile_text
    assert (
        '"specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"'
        in dockerfile_text
    )
    assert "https://api.github.com/meta" in dockerfile_text
    assert "test -s /root/.ssh/known_hosts" in dockerfile_text
    assert "git+ssh://" not in dockerfile_text


def test_container_init_can_skip_agent_installs(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False, no_agents=True)

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert (
        '"specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"'
        in dockerfile_text
    )
    assert "RUN npm install -g @anthropic-ai/claude-code @openai/codex" not in dockerfile_text
    assert "# RUN npm install -g @anthropic-ai/claude-code" in dockerfile_text
    assert "# RUN npm install -g @openai/codex" in dockerfile_text


def test_container_init_can_override_public_source_repository(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="git@github.com:acme/spec-fork.git",
    )

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "ARG SPEC_BUTLER_REPOSITORY_URL=https://github.com/acme/spec-fork.git" in dockerfile_text


def test_container_init_strips_source_repository_credentials_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="https://oauth2:secret@github.com/acme/spec.git",
    )

    assert container.cmd_init(args) == 0
    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "oauth2:secret" not in dockerfile_text
    assert "ARG SPEC_BUTLER_REPOSITORY_URL=https://github.com/acme/spec.git" in dockerfile_text
    assert capsys.readouterr().err == ""


def test_container_init_rejects_invalid_source_repository_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="file:///private/source",
    )

    assert container.cmd_init(args) == 1
    assert not (tmp_path / ".spec").exists()
    assert "invalid --source-repository" in capsys.readouterr().err


def test_container_init_fails_closed_when_source_repository_is_unknown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False)

    with patch("spec_runtime.container.runtime_repository_https_url", return_value=""):
        assert container.cmd_init(args) == 1

    assert not (tmp_path / ".spec").exists()
    assert "--source-repository" in capsys.readouterr().err


def test_smoke_uses_autopilot_container_rollout_policy(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="worktree",
            backend_explicit=False,
            container=ContainerExecutionConfig(image="example/spec-worker:latest"),
        ),
        autopilot=AutopilotConfig(container_default_enabled=True),
    )
    backend = FakeBackend()
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        no_bootstrap=False,
        verify_gates=False,
        timeout=300,
    )

    with (
        patch("spec_runtime.container.load_repo_spec_runtime_config", return_value=config),
        patch("spec_runtime.container.get_execution_backend", return_value=backend) as get_backend,
        patch("spec_runtime.container.run_smoke", return_value=0) as run_smoke,
    ):
        code = container.cmd_smoke(args)

    assert code == 0
    smoke_config = get_backend.call_args.args[0]
    assert smoke_config.execution.backend == "container"
    assert smoke_config.execution.backend_explicit is False
    run_smoke.assert_called_once()


def test_smoke_no_bootstrap_clears_backend_bootstrap_commands(tmp_path: Path) -> None:
    config = _config(
        bootstrap_install_command="python -m pip install -e .",
        bootstrap_cache=BootstrapCacheConfig(enabled=True, command="python -m pip install -e ."),
    )
    backend = FakeBackend()
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        no_bootstrap=True,
        verify_gates=False,
        timeout=300,
    )

    with (
        patch("spec_runtime.container.load_repo_spec_runtime_config", return_value=config),
        patch("spec_runtime.container.get_execution_backend", return_value=backend) as get_backend,
        patch("spec_runtime.container.run_smoke", return_value=0) as run_smoke,
    ):
        code = container.cmd_smoke(args)

    assert code == 0
    smoke_config = get_backend.call_args.args[0]
    assert smoke_config.bootstrap_install_command == ""
    assert smoke_config.bootstrap_cache.enabled is False
    assert smoke_config.bootstrap_cache.command == ""
    assert run_smoke.call_args.kwargs["run_bootstrap"] is False


def test_smoke_invokes_backend_and_cleans_up() -> None:
    backend = FakeBackend()
    config = _config(
        agents=AgentConfig(default="codex", allowed=("codex",)),
        bootstrap_install_command="python -m pip install -e .",
    )

    code = container.run_smoke(
        Path("/tmp/repo"),
        config,
        backend=backend,  # type: ignore[arg-type]
        run_bootstrap=True,
        run_verify=False,
    )

    assert code == 0
    assert backend.cleaned is True
    assert backend.cleanup_allow_unpushed_work is True
    argv = [request.argv for request in backend.commands]
    assert ["git", "--version"] in argv
    assert any(
        request.argv[0:2] == ["sh", "-lc"] and "codex --version" in request.argv[2] for request in backend.commands
    )


def test_smoke_cleans_up_after_command_failure() -> None:
    class FailingBackend(FakeBackend):
        def run_command(self, request: CommandRequest):
            self.commands.append(request)
            return type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": "boom", "argv": request.argv},
            )()

    backend = FailingBackend()

    code = container.run_smoke(
        Path("/tmp/repo"),
        _config(),
        backend=backend,  # type: ignore[arg-type]
    )

    assert code == 1
    assert backend.cleaned is True
    assert backend.cleanup_allow_unpushed_work is True


def test_smoke_rejects_worker_spec_version_drift(capsys: pytest.CaptureFixture[str]) -> None:
    class StaleSpecBackend(FakeBackend):
        def run_command(self, request: CommandRequest):
            result = super().run_command(request)
            if request.argv == ["spec", "--version"]:
                result.stdout = "0.0.1\n"
            return result

    backend = StaleSpecBackend()

    code = container.run_smoke(
        Path("/tmp/repo"),
        _config(),
        backend=backend,  # type: ignore[arg-type]
    )

    assert code == 1
    assert backend.cleaned is True
    assert "spec version mismatch" in capsys.readouterr().err


def test_smoke_rejects_same_version_from_different_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class StaleSourceBackend(FakeBackend):
        def run_command(self, request: CommandRequest):
            result = super().run_command(request)
            if request.argv == ["spec", "--source-id"]:
                result.stdout = f"{host_spec_runtime_version()}@{'0' * 40}\n"
            return result

    backend = StaleSourceBackend()

    # Installed wheels legitimately have no Git commit in their source ID.
    # Pin an editable-checkout identity explicitly so this unit test exercises
    # commit drift independent of how the test runner installed Spec Butler.
    with patch(
        "spec_runtime.container.host_spec_runtime_source_id",
        return_value=f"{host_spec_runtime_version()}@{'1' * 40}",
    ):
        code = container.run_smoke(
            Path("/tmp/repo"),
            _config(),
            backend=backend,  # type: ignore[arg-type]
        )

    assert code == 1
    assert backend.cleaned is True
    assert "spec source identity mismatch" in capsys.readouterr().err


def test_host_source_identity_marks_dirty_editable_checkout() -> None:
    completed = [
        subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, "abc123\n", ""),
        subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, " M file.py\n", ""),
    ]

    with (
        patch("spec_runtime.execution_backend.host_spec_runtime_version", return_value="1.2.3"),
        patch("importlib.metadata.distribution", side_effect=RuntimeError("no metadata")),
        patch("spec_runtime.execution_backend._is_adjacent_spec_runtime_checkout", return_value=True),
        patch("spec_runtime.execution_backend.run_git", side_effect=completed),
    ):
        source_id = host_spec_runtime_source_id()

    assert source_id == "1.2.3@abc123+dirty"


def test_host_source_identity_does_not_use_project_around_installed_wheel(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("user project\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    wheel_module = (
        tmp_path
        / ".venv"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "spec_runtime"
        / "execution_backend.py"
    )
    wheel_module.parent.mkdir(parents=True)
    wheel_module.touch()

    with (
        patch("spec_runtime.execution_backend.__file__", str(wheel_module)),
        patch("spec_runtime.execution_backend.host_spec_runtime_version", return_value="1.2.3"),
        patch("importlib.metadata.distribution", side_effect=RuntimeError("no metadata")),
    ):
        source_id = host_spec_runtime_source_id()

    assert source_id == "1.2.3"


def test_autopilot_container_backend_error_points_to_doctor() -> None:
    from spec_runtime.autopilot import AutopilotBackendPolicy, validate_autopilot_backend

    config = _config(
        autopilot=AutopilotConfig(container_default_enabled=True),
    )
    policy = AutopilotBackendPolicy(
        backend="container",
        safety_mode="safe",
        source="rollout-policy",
        backend_explicit=False,
    )

    with patch("shutil.which", return_value=None):
        message = validate_autopilot_backend(policy, config)

    assert "spec container doctor" in message


def test_worker_dockerfile_spec_install_layer_cache_busts_on_version() -> None:
    """The pip layer must reference SPEC_BUTLER_VERSION so
    Docker's layer cache keys on the host spec version."""
    for build_ssh in (False, True):
        rendered = container.render_worker_dockerfile(("claude",), build_ssh=build_ssh)
        assert "ARG SPEC_BUTLER_VERSION=unpinned" in rendered
        arg_pos = rendered.index("ARG SPEC_BUTLER_VERSION")
        use_pos = rendered.index('echo "specbutler ${SPEC_BUTLER_VERSION}"')
        install_pos = rendered.index("specbutler @ git+")
        assert arg_pos < use_pos < install_pos
        # The version reference must live in the same RUN as the install, or
        # the cache bust would not invalidate the pip layer.
        run_start = rendered.rindex("RUN", 0, use_pos)
        assert rendered.index("specbutler @ git+", run_start) == install_pos


def test_worker_dockerfile_can_pin_detected_agent_cli_versions() -> None:
    rendered = container.render_worker_dockerfile(
        ("claude", "codex"),
        agent_versions={"claude": "2.1.233", "codex": "0.147.0"},
    )

    assert "RUN npm install -g @anthropic-ai/claude-code@2.1.233 @openai/codex@0.147.0" in rendered


@pytest.mark.parametrize(
    ("agent", "output", "expected"),
    [
        ("claude", "2.1.233 (Claude Code)\n", "2.1.233"),
        ("codex", "codex-cli 0.147.0\n", "0.147.0"),
    ],
)
def test_detect_agent_cli_version(agent: str, output: str, expected: str) -> None:
    with (
        patch("spec_runtime.container.shutil.which", return_value=f"/usr/bin/{agent}"),
        patch(
            "spec_runtime.container.subprocess.run",
            return_value=subprocess.CompletedProcess([agent, "--version"], 0, output, ""),
        ),
    ):
        assert container._detect_agent_cli_version(agent) == expected


def test_host_spec_runtime_version_reads_pyproject() -> None:
    from spec_runtime.execution_backend import host_spec_runtime_version

    version = host_spec_runtime_version()
    assert version and version != "unknown"
    assert version[0].isdigit()
