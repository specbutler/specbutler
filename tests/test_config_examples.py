from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from spec_runtime.config import SpecConfigError, load_repo_spec_runtime_config

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_EXAMPLE = REPO_ROOT / "examples" / "spec.toml"
PACKAGED_EXAMPLE = REPO_ROOT / "src" / "spec_runtime" / "examples" / "spec.toml"


def test_public_and_packaged_starter_configs_stay_identical() -> None:
    assert PUBLIC_EXAMPLE.read_text() == PACKAGED_EXAMPLE.read_text()


def test_starter_config_is_valid_runtime_configuration(tmp_path: Path) -> None:
    raw = PUBLIC_EXAMPLE.read_text()
    assert isinstance(tomllib.loads(raw), dict)
    (tmp_path / ".spec.toml").write_text(raw)

    config = load_repo_spec_runtime_config(tmp_path)

    assert config.base_ref == "origin/main"
    assert config.agents.default == "claude"
    assert {gate.name for gate in config.verify_gates} == {"test", "lint"}


def test_gate_review_evidence_paths_are_parsed_as_an_exact_allowlist(
    tmp_path: Path,
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[[verify.gates]]
name = "e2e"
command = "pytest tests/e2e"
review_evidence = ["artifacts/native.json", "artifacts\\\\summary.txt"]
""".lstrip(),
        encoding="utf-8",
    )

    config = load_repo_spec_runtime_config(tmp_path)

    assert config.verify_gates[0].review_evidence == (
        "artifacts/native.json",
        "artifacts/summary.txt",
    )


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside.txt", "/absolute.txt", "C:\\\\absolute.txt"],
)
def test_gate_review_evidence_rejects_paths_outside_the_workspace(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    rendered_path = unsafe_path.replace("\\", "\\\\")
    (tmp_path / ".spec.toml").write_text(
        f"""
[[verify.gates]]
name = "e2e"
command = "pytest tests/e2e"
review_evidence = ["{rendered_path}"]
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SpecConfigError, match="must stay within"):
        load_repo_spec_runtime_config(tmp_path)
