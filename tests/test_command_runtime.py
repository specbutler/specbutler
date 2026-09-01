from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from spec_runtime.command_runtime import (
    CommandConfigurationError,
    CommandSpec,
    parse_command_variants,
    run_command,
)


def test_argv_preserves_difficult_arguments_in_real_process(tmp_path: Path) -> None:
    helper = tmp_path / "argv helper.py"
    output = tmp_path / "received.json"
    helper.write_text(
        "import json, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
    )
    expected = ["with space", 'a"quote', "snowman-☃", r"C:\\path\\", "$HOME", "x&y"]
    result = run_command(
        CommandSpec("argv", (sys.executable, str(helper), str(output), *expected)),
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert json.loads(output.read_text()) == expected


def test_windows_override_precedes_portable_argv() -> None:
    variants = parse_command_variants(
        {"argv": ["python", "posix.py"], "argv_windows": ["python", "win.py"]},
        source="test",
    )
    assert variants.select(windows=False).value == ("python", "posix.py")
    assert variants.select(windows=True).value == ("python", "win.py")


def test_named_command_uses_matching_named_argv_keys() -> None:
    variants = parse_command_variants(
        {"install_argv": ["python", "install.py"], "install_argv_windows": ["py", "install.py"]},
        command_key="install_command",
        source="[bootstrap]",
    )
    assert variants.select(windows=False).value[0] == "python"
    assert variants.select(windows=True).value[0] == "py"


def test_windows_script_requires_declared_shell() -> None:
    with pytest.raises(CommandConfigurationError, match="requires shell_windows"):
        parse_command_variants({"command_windows": "Write-Host ok"}, source="test")


def test_display_redacts_secrets_and_uses_platform_quoting() -> None:
    command = CommandSpec("argv", ("tool", "with space", "--token=supersecret"))
    assert "supersecret" not in command.display(windows=False)
    assert '"with space"' in command.display(windows=True)
