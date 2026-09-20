from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runtime import orchestrator as orch


@pytest.mark.parametrize("backend_name", ["worktree", "clone"])
def test_host_scratch_uses_existing_launch_grant_and_cleanup(tmp_path, backend_name):
    outbox = orch._prepare_scoped_agent_completion_outbox(tmp_path, "scratch-run", 1)
    env = {name: "/unwritable" for name in ("TMPDIR", "TMP", "TEMP")}
    backend = SimpleNamespace(identity=SimpleNamespace(backend=backend_name))
    try:
        orch._apply_host_implement_temp_environment(env, backend, outbox)
        scratch = Path(env["TMPDIR"])
        assert env["TMP"] == env["TEMP"] == str(scratch)
        assert scratch.parent == outbox.parent
        assert not scratch.is_relative_to(tmp_path)
        (scratch / "probe").write_text("writable")
    finally:
        orch._cleanup_scoped_agent_completion_outbox(tmp_path, "scratch-run", 1)
    assert not scratch.exists()


def test_container_keeps_worker_temporary_paths(tmp_path):
    env = {name: "/worker/tmp" for name in ("TMPDIR", "TMP", "TEMP")}
    backend = SimpleNamespace(identity=SimpleNamespace(backend="container"))
    orch._apply_host_implement_temp_environment(env, backend, tmp_path / "result.json")
    assert set(env.values()) == {"/worker/tmp"}
    assert not (tmp_path / "tmp").exists()
