from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LABCTL = REPO_ROOT / "tools" / "windows-lab" / "labctl"


@pytest.mark.windows_real_provider
def test_windows_real_provider_release_proof() -> None:
    """Run the checked-in, credential-free controller against an explicit lab."""
    config_value = os.environ.get("SPEC_WINDOWS_LAB_CONFIG")
    if os.environ.get("SPEC_WINDOWS_REAL_PROVIDER") != "1" or not config_value:
        pytest.skip(
            "set SPEC_WINDOWS_REAL_PROVIDER=1 and SPEC_WINDOWS_LAB_CONFIG to opt into the real Windows proof"
        )
    config = Path(config_value).expanduser().resolve()
    if not config.is_file():
        pytest.skip(f"explicit Windows lab config does not exist: {config}")

    subprocess.run(
        [str(LABCTL), "proof"],
        cwd=REPO_ROOT,
        env={**os.environ, "SPEC_WINDOWS_LAB_CONFIG": str(config)},
        check=True,
        timeout=5 * 60 * 60,
    )
