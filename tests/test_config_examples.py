from __future__ import annotations

import tomllib
from pathlib import Path

from spec_runtime.config import load_repo_spec_runtime_config

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
