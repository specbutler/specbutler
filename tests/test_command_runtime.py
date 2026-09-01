from __future__ import annotations

import json
import os
import subprocess
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


@pytest.mark.parametrize(
    "flag",
    ["--token", "--github-token", "--api_key", "--client-secret", "--password"],
)
def test_display_redacts_secret_in_split_option_form(flag: str) -> None:
    display = CommandSpec("argv", ("tool", flag, "supersecret", "visible")).display(
        windows=False
    )
    assert "supersecret" not in display
    assert "visible" in display
    assert "***" in display


@pytest.mark.parametrize("shell", ["sh", "powershell", "pwsh", "cmd"])
@pytest.mark.parametrize(
    "script",
    [
        "tool --token supersecret --name visible",
        'tool --client-secret "secret with spaces" --name visible',
        "tool --api_key='supersecret' --name visible",
    ],
)
def test_script_display_redacts_embedded_secrets(shell: str, script: str) -> None:
    display = CommandSpec("script", script, shell).display(
        windows=shell != "sh"
    )
    assert "supersecret" not in display
    assert "secret with spaces" not in display
    assert "visible" in display
    assert "***" in display


def test_posix_login_shell_is_explicit() -> None:
    command = CommandSpec("script", "tool", "sh", login_shell=True)
    assert command.argv(windows=False) == ["sh", "-lc", "tool"]


def test_native_windows_rejects_portable_shell_instead_of_assuming_git_bash() -> None:
    selected = parse_command_variants(
        {"command": "python -m venv .venv && .venv/bin/python -m pip install -e ."},
        source="[bootstrap]",
    ).select(windows=True)
    assert selected is not None
    with pytest.raises(FileNotFoundError, match="cannot run on native Windows"):
        selected.argv(windows=True)


def test_native_windows_runs_simple_legacy_command_as_direct_argv() -> None:
    selected = parse_command_variants(
        {"command": 'python -m helper "path with space"'}, source="[bootstrap]"
    ).select(windows=True)
    assert selected is not None
    assert selected.mode == "argv"
    assert selected.argv(windows=True) == ["python", "-m", "helper", "path with space"]


@pytest.mark.parametrize(
    ("shell", "executable", "prefix"),
    [
        ("powershell", "powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]),
        ("pwsh", "pwsh", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]),
    ],
)
def test_windows_shell_argv_preserves_script_and_metadata(
    shell: str, executable: str, prefix: list[str]
) -> None:
    command = CommandSpec("script", "Write-Output $args", shell)  # type: ignore[arg-type]
    argv = command.argv(
        which=lambda name: name,
        windows=True,
        arguments=("C:\\path with space\\", 'a"quote', "snowman-☃", "$HOME", "x&y"),
    )
    assert argv == [
        executable,
        *prefix,
        "& { Write-Output $args } 'C:\\path with space\\' 'a\"quote' "
        "'snowman-☃' '$HOME' 'x&y'",
    ]


def test_cmd_hook_metadata_requires_environment_or_direct_argv() -> None:
    command = CommandSpec("script", "echo ok", "cmd")
    with pytest.raises(CommandConfigurationError, match="SPEC_ID.*argv_windows"):
        command.argv(windows=True, arguments=("path with space", "x&y"))


def test_cmd_launch_materializes_and_removes_batch_file(tmp_path: Path) -> None:
    command = CommandSpec("script", '"C:\\Program Files\\tool.exe" "path with space"', "cmd")
    with command.launch_argv(cwd=tmp_path, windows=True) as argv:
        assert argv[:3] == ["cmd.exe", "/d", "/c"]
        script_path = Path(argv[3])
        assert script_path.suffix == ".cmd"
        assert script_path.read_text().strip() == str(command.value)
        assert str(command.value) not in argv
    assert not script_path.exists()


def test_cmd_batch_file_is_removed_after_timeout(tmp_path: Path) -> None:
    launched_path: Path | None = None
    with pytest.raises(subprocess.TimeoutExpired):
        command = CommandSpec("script", "echo ok", "cmd")
        with command.launch_argv(cwd=tmp_path, windows=True) as argv:
            launched_path = Path(argv[3])
            assert launched_path.exists()
            raise subprocess.TimeoutExpired(argv, 1)
    assert launched_path is not None
    assert not launched_path.exists()


def test_missing_declared_powershell_is_targeted() -> None:
    command = CommandSpec("script", "Write-Output ok", "pwsh")
    with pytest.raises(FileNotFoundError, match="declared shell 'pwsh' is not installed"):
        command.argv(which=lambda _name: None, windows=True)


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
def test_sh_hook_metadata_starts_at_one_in_real_process(tmp_path: Path) -> None:
    output = tmp_path / "hook metadata.json"
    command = CommandSpec(
        "script",
        f"{shlex_quote(sys.executable)} -c 'import json, pathlib, sys; "
        f"pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))' \"$@\"",
        "sh",
    )
    expected = ["spec-id", "run-id", "path with space"]
    completed = subprocess.run(
        command.argv(arguments=(str(output), *expected)),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text()) == expected


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows PowerShell")
def test_powershell_hook_metadata_is_available_in_real_process(tmp_path: Path) -> None:
    output = tmp_path / "hook metadata.json"
    command = CommandSpec(
        "script",
        "$values = @($args | Select-Object -Skip 1); "
        "[IO.File]::WriteAllText($args[0], "
        "(ConvertTo-Json -Compress -InputObject $values))",
        "powershell",
    )
    expected = [
        "path with space",
        "percent% and $dollar",
        "semi; amp&",
        "single' and double\"",
        "snowman-☃",
        "C:\\trailing\\",
    ]
    completed = subprocess.run(
        command.argv(arguments=(str(output), *expected)),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding="utf-8-sig")) == expected


@pytest.mark.skipif(os.name != "nt", reason="requires native cmd.exe")
def test_cmd_hook_metadata_is_lossless_via_environment(tmp_path: Path) -> None:
    output = tmp_path / "hook metadata.json"
    helper = tmp_path / "read metadata.py"
    helper.write_text(
        "import json, os, pathlib, sys\n"
        "keys = ('SPEC_ID', 'SPEC_RUN_ID', 'SPEC_PATH', 'SPEC_WORKTREE')\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps([os.environ[k] for k in keys]))\n",
        encoding="utf-8",
    )
    expected = [
        "space percent% $dollar",
        "semi; amp&",
        "single' double\" snowman-☃",
        "C:\\trailing\\",
    ]
    env = os.environ.copy()
    env.update(dict(zip(("SPEC_ID", "SPEC_RUN_ID", "SPEC_PATH", "SPEC_WORKTREE"), expected)))
    command = CommandSpec(
        "script",
        f'"{sys.executable}" "{helper}" "{output}"',
        "cmd",
    )
    completed = run_command(command, cwd=tmp_path, env=env)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text()) == expected


def shlex_quote(value: str) -> str:
    """Keep the real-process fixture readable without changing production quoting."""
    import shlex

    return shlex.quote(value)
