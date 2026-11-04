from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "tools" / "ci_pip_install.py"


@pytest.fixture
def installer():
    spec = importlib.util.spec_from_file_location("ci_pip_install", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("returncodes,expected,delays", [
    ([0], 0, []),
    ([1, 0], 0, [5]),
    ([1, 1, 0], 0, [5, 10]),
    ([1, 2, 7], 7, [5, 10]),
    ([-15], 143, []),
])
def test_ci_install_retries_are_bounded_and_preserve_exit_status(
    installer, monkeypatch: pytest.MonkeyPatch, returncodes: list[int], expected: int, delays: list[int],
):
    attempts = []
    waits = []
    outcomes = iter(returncodes)
    args = ["-e", "./project with spaces[dev,tui,web]"]

    def run(command, *, check):
        attempts.append(command)
        return SimpleNamespace(returncode=next(outcomes))

    monkeypatch.setattr(installer, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(installer, "time", SimpleNamespace(sleep=waits.append))
    assert installer.main(args) == expected
    assert attempts == [[sys.executable, "-m", "pip", "install", *args]] * len(returncodes)
    assert waits == delays


def test_ci_install_does_not_retry_keyboard_interrupt(installer, monkeypatch: pytest.MonkeyPatch):
    def interrupted_run(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "subprocess", SimpleNamespace(run=interrupted_run))
    monkeypatch.setattr(installer, "time", SimpleNamespace(sleep=lambda _: pytest.fail("cancelled install retried")))
    with pytest.raises(KeyboardInterrupt):
        installer.main(["example"])


def test_ci_install_cli_uses_its_interpreter_and_forwards_requirement_arguments(tmp_path: Path):
    output = tmp_path / "pip-arguments.json"
    (tmp_path / "pip.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CI_PIP_TEST_OUTPUT']).write_text(\n"
        "    json.dumps({'executable': sys.executable, 'args': sys.argv[1:]}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    requirement = str(tmp_path / "release distribution.whl") + "[dev,tui,web]"
    result = subprocess.run(
        [sys.executable, str(HELPER), requirement, "--no-deps"],
        env={**os.environ, "PYTHONPATH": str(tmp_path), "CI_PIP_TEST_OUTPUT": str(output)},
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "executable": sys.executable, "args": ["install", requirement, "--no-deps"],
    }


def test_ci_workflow_uses_retrying_installer_for_every_pip_install():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    scripts = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert sum(script.count("tools/ci_pip_install.py") for script in scripts) == 8
    assert all("pip install" not in script for script in scripts)
    # Keep wheel installs inside the wheel environment, and Windows installs
    # inside the release-distribution environment rather than the build Python.
    assert any('"$RUNNER_TEMP/spec-wheel-smoke/bin/python" tools/ci_pip_install.py' in script for script in scripts)
    assert any('"$env:RUNNER_TEMP/spec-windows-probe/Scripts/python.exe" tools/ci_pip_install.py' in script for script in scripts)
