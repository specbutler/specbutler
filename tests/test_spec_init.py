"""Tests for spec init command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PureWindowsPath
from unittest.mock import MagicMock, patch

import pytest

from spec_runtime.config import (
    SpecConfigError,
    SpecConfigNotFoundError,
    _discover_repo_root,
    load_spec_runtime_config,
)
from spec_runtime.init import (
    _ask_agent_for_config,
    _build_agent_merge_command,
    _build_yolo_prompt,
    _copy_template,
    _detect_agents,
    _detect_base_branch,
    _detect_implement_commands,
    _detect_install_command,
    _detect_verify_gates,
    _gather_repo_context,
    _generate_spec_toml,
    _git_repo_root,
    _merge_file_with_agent,
    _read_bundled_template,
    _toml_escape,
    _update_gitignore,
    cmd_init,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Config enforcement
# ---------------------------------------------------------------------------


class TestConfigEnforcement:
    def test_require_true_raises_when_no_config(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("spec_runtime.config._config_path", return_value=tmp_path / ".spec.toml"):
            with pytest.raises(SpecConfigNotFoundError, match="spec init"):
                load_spec_runtime_config(require=True)

    def test_require_false_returns_defaults(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("spec_runtime.config._config_path", return_value=tmp_path / ".spec.toml"):
            config = load_spec_runtime_config(require=False)
            assert config.base_ref == "origin/master"

    def test_load_reads_implement_section(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
base_ref = "origin/main"

[agents]
review_default = "codex"

[implement]
setup_command = "scripts/implement-setup.sh"
teardown_command = "scripts/implement-teardown.sh"
"""
        )
        with patch("spec_runtime.config._config_path", return_value=tmp_path / ".spec.toml"):
            config = load_spec_runtime_config(require=True)

        assert config.implement.setup_command == "scripts/implement-setup.sh"
        assert config.implement.teardown_command == "scripts/implement-teardown.sh"
        assert config.agents.review_default == "codex"

    def test_load_reads_mcp_allow_from_user(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
base_ref = "origin/main"

[mcp]
allow_from_user = ["render", "kubectl"]
"""
        )
        with patch("spec_runtime.config._config_path", return_value=tmp_path / ".spec.toml"):
            config = load_spec_runtime_config(require=True)
        assert config.mcp.allow_from_user == ("render", "kubectl")

    def test_load_rejects_non_list_mcp_allow_from_user(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
base_ref = "origin/main"

[mcp]
allow_from_user = "render"
"""
        )
        with patch("spec_runtime.config._config_path", return_value=tmp_path / ".spec.toml"):
            with pytest.raises(SpecConfigError, match="allow_from_user must be a list of strings"):
                load_spec_runtime_config(require=True)

    @pytest.mark.parametrize(
        "windows_variant",
        [
            'command_windows = "Write-Output ok"\nshell_windows = "powershell"',
            'argv_windows = ["py", "-m", "pytest"]',
        ],
    )
    def test_load_rejects_windows_only_verify_gate_on_posix(
        self, tmp_path: Path, windows_variant: str
    ) -> None:
        (tmp_path / ".spec.toml").write_text(
            f"""
[verify]
[[verify.gates]]
name = "test"
{windows_variant}
"""
        )

        with pytest.raises(
            SpecConfigError,
            match=r"verify\.gates.*requires command or argv.*additive overrides",
        ):
            load_spec_runtime_config(config_path=tmp_path / ".spec.toml")

    def test_discover_repo_root_prefers_current_repo_markers(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        nested = repo_root / "pkg" / "feature"
        nested.mkdir(parents=True)
        (repo_root / ".git").mkdir()

        monkeypatch.chdir(nested)

        with patch("spec_runtime.config.subprocess.run") as run:
            assert _discover_repo_root() == repo_root
        run.assert_not_called()

    @pytest.mark.parametrize(
        "installed_module",
        (
            "venv/lib/python3.11/site-packages/spec_runtime/config.py",
            "venv/Lib/site-packages/spec_runtime/config.py",
        ),
        ids=("posix-wheel", "windows-wheel"),
    )
    def test_discover_repo_root_never_uses_installed_package_location(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        installed_module: str,
    ) -> None:
        working_directory = tmp_path / "unconfigured-project"
        working_directory.mkdir()
        module_path = tmp_path / installed_module
        module_path.parent.mkdir(parents=True)
        module_path.write_text("# synthetic installed module\n")
        monkeypatch.chdir(working_directory)
        path_exists = Path.exists
        path_is_file = Path.is_file

        def without_git_markers(path: Path) -> bool:
            return False if path.name == ".git" else path_exists(path)

        def without_spec_config(path: Path) -> bool:
            return False if path.name == ".spec.toml" else path_is_file(path)

        with patch("spec_runtime.config.__file__", str(module_path)), patch(
            "spec_runtime.config.Path.exists",
            without_git_markers,
        ), patch(
            "spec_runtime.config.Path.is_file",
            without_spec_config,
        ), patch(
            "spec_runtime.config.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                returncode=128,
                cmd=["git", "rev-parse", "--show-toplevel"],
            ),
        ):
            repo_root = _discover_repo_root()

        assert repo_root == working_directory.resolve()
        assert "site-packages" not in str(repo_root)

    def test_git_repo_root_decodes_git_for_windows_output_as_utf8(self, tmp_path: Path) -> None:
        expected = tmp_path / "Spec Butler snow-雪"
        stdout = f"{expected}\n".encode("utf-8")

        def windows_locale_runner(command, **kwargs):  # noqa: ANN001
            # Reproduce Python on an English Windows host: without an explicit
            # encoding, subprocess would use cp1252 for Git's UTF-8 bytes.
            encoding = kwargs.get("encoding") or "cp1252"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=stdout.decode(encoding),
                stderr="",
            )

        with patch("spec_runtime.git_common.subprocess.run", side_effect=windows_locale_runner):
            assert _git_repo_root() == expected

    def test_git_repo_root_preserves_real_unicode_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root = tmp_path / "Spec Butler snow-雪"
        repo_root.mkdir()
        _init_git_repo(repo_root)
        monkeypatch.chdir(repo_root)

        assert _git_repo_root() == repo_root

    def test_cli_main_rejects_commands_without_config(self):
        from spec_runtime.cli import main

        with patch("spec_runtime.cli._lazy_config", side_effect=SpecConfigNotFoundError("no config")):
            rc = main(["list"])
        assert rc == 1

    def test_cli_main_allows_init_without_config(self, tmp_path):
        _init_git_repo(tmp_path)
        from spec_runtime.cli import main

        with (
            patch("spec_runtime.init._git_repo_root", return_value=tmp_path),
            patch(
                "spec_runtime.init._detect_agents",
                return_value=("claude", ["claude"]),
            ),
        ):
            rc = main(["init"])
        assert rc == 0
        assert (tmp_path / ".spec.toml").exists()


# ---------------------------------------------------------------------------
# Agent detection
# ---------------------------------------------------------------------------


class TestDetectAgents:
    def test_both_available(self, monkeypatch):
        monkeypatch.setattr("spec_runtime.init.shutil.which", lambda name: f"/usr/bin/{name}")
        default, allowed = _detect_agents()
        assert default == "claude"
        assert allowed == ["claude", "codex"]

    def test_only_claude(self, monkeypatch):
        monkeypatch.setattr(
            "spec_runtime.init.shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        default, allowed = _detect_agents()
        assert default == "claude"
        assert allowed == ["claude"]

    def test_only_codex(self, monkeypatch):
        monkeypatch.setattr(
            "spec_runtime.init.shutil.which",
            lambda name: "/usr/bin/codex" if name == "codex" else None,
        )
        default, allowed = _detect_agents()
        assert default == "codex"
        assert allowed == ["codex"]

    def test_none_available(self, monkeypatch):
        monkeypatch.setattr("spec_runtime.init.shutil.which", lambda name: None)
        default, allowed = _detect_agents()
        assert default == ""
        assert allowed == []


# ---------------------------------------------------------------------------
# Verify gate detection
# ---------------------------------------------------------------------------


class TestDetectVerifyGates:
    def test_detects_pytest_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        gates = _detect_verify_gates(tmp_path)
        assert any(
            g["name"] == "test" and g["command"] == ".venv/bin/python -m pytest"
            for g in gates
        )
        test_gate = next(g for g in gates if g["name"] == "test")
        assert test_gate["argv_windows"] == [
            ".venv/Scripts/python.exe",
            "-m",
            "pytest",
        ]

    def test_detects_ruff_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py311"\n')
        gates = _detect_verify_gates(tmp_path)
        assert any(
            g["name"] == "lint" and g["command"] == ".venv/bin/python -m ruff check ."
            for g in gates
        )
        lint_gate = next(g for g in gates if g["name"] == "lint")
        assert lint_gate["argv_windows"] == [
            ".venv/Scripts/python.exe",
            "-m",
            "ruff",
            "check",
            ".",
        ]

    def test_detects_makefile_targets(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n\nlint:\n\truff check .\n")
        gates = _detect_verify_gates(tmp_path)
        names = [g["name"] for g in gates]
        assert "test" in names
        assert "lint" in names

    def test_detects_package_json_scripts(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
        gates = _detect_verify_gates(tmp_path)
        assert any(g["name"] == "test" and g["command"] == "npm run test" for g in gates)

    def test_pyproject_takes_precedence_over_makefile(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        gates = _detect_verify_gates(tmp_path)
        test_gates = [g for g in gates if g["name"] == "test"]
        assert len(test_gates) == 1
        assert test_gates[0]["command"] == ".venv/bin/python -m pytest"

    def test_make_install_keeps_project_controlled_python_gate(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
        )
        (tmp_path / "Makefile").write_text("install:\n\ttrue\n")

        gates = _detect_verify_gates(tmp_path)

        assert any(g["name"] == "test" and g["command"] == "pytest" for g in gates)

    def test_no_project_files(self, tmp_path):
        gates = _detect_verify_gates(tmp_path)
        assert gates == []

    def test_e2e_gate_is_not_parallel(self, tmp_path):
        (tmp_path / "Makefile").write_text("e2e:\n\tplaywright test\n")
        gates = _detect_verify_gates(tmp_path)
        e2e = [g for g in gates if g["name"] == "e2e"]
        assert len(e2e) == 1
        assert e2e[0]["parallel"] is False


class TestDetectInstallCommand:
    @pytest.mark.parametrize("marker", ["pyproject.toml", "setup.py"])
    def test_python_project_uses_per_worktree_virtualenv(self, tmp_path, marker):
        (tmp_path / marker).write_text("\n")

        command = _detect_install_command(tmp_path)

        assert command.startswith("python -m venv .venv && ")
        assert ".venv/bin/python -m pip install -e ." in command
        assert not command.startswith("pip ")

    def test_installs_detected_verify_tools_without_dev_extra(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
            "[tool.ruff]\n"
            'target-version = "py311"\n'
        )

        command = _detect_install_command(tmp_path)

        assert command.endswith("pip install -e . pytest ruff")

    def test_installs_declared_dev_extra(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "example"\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest", "ruff"]\n'
            "[tool.pytest.ini_options]\n"
            "[tool.ruff]\n"
        )

        command = _detect_install_command(tmp_path)

        assert command.endswith("pip install -e '.[dev]'")
        assert "'.[dev]' pytest" not in command

    def test_installs_detected_verify_tool_missing_from_dev_extra(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "example"\n'
            "[project.optional-dependencies]\n"
            'dev = ["mypy", "pytest>=8"]\n'
            "[tool.pytest.ini_options]\n"
            "[tool.ruff]\n"
        )

        command = _detect_install_command(tmp_path)

        assert command.endswith("pip install -e '.[dev]' ruff")

    def test_installs_detected_tool_when_dev_requirement_marker_is_false(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "example"\n'
            "[project.optional-dependencies]\n"
            'dev = ["ruff; python_version < \'3.0\'"]\n'
            "[tool.ruff]\n"
        )

        command = _detect_install_command(tmp_path)

        assert command.endswith("pip install -e '.[dev]' ruff")


class TestDetectImplementCommands:
    def test_detects_setup_and_teardown_scripts(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "implement-setup.sh").write_text("#!/usr/bin/env bash\n")
        (scripts / "implement-teardown.sh").write_text("#!/usr/bin/env bash\n")

        setup_command, teardown_command = _detect_implement_commands(tmp_path)

        assert setup_command == "scripts/implement-setup.sh"
        assert teardown_command == "scripts/implement-teardown.sh"

    def test_detects_python_helpers_as_python_commands(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "implement-setup.py").write_text("print('setup')\n")
        (scripts / "implement-teardown.py").write_text("print('teardown')\n")

        setup_command, teardown_command = _detect_implement_commands(tmp_path)

        assert setup_command == "python scripts/implement-setup.py"
        assert teardown_command == "python scripts/implement-teardown.py"


# ---------------------------------------------------------------------------
# Base branch detection
# ---------------------------------------------------------------------------


class TestDetectBaseBranch:
    def test_falls_back_to_local_branch_without_remote(self, tmp_path):
        _init_git_repo(tmp_path)
        result = _detect_base_branch(tmp_path)
        # No remote — uses local branch name without origin/ prefix
        assert result == "main"


# ---------------------------------------------------------------------------
# TOML generation
# ---------------------------------------------------------------------------


class TestGenerateSpecToml:
    def test_generates_valid_toml(self):
        import tomllib

        content = _generate_spec_toml(
            base_ref="origin/main",
            default_agent="claude",
            review_default="codex",
            allowed_agents=["claude", "codex"],
            gates=[{"name": "test", "command": "pytest", "parallel": True}],
            setup_command="scripts/implement-setup.sh",
            teardown_command="scripts/implement-teardown.sh",
        )
        parsed = tomllib.loads(content)
        assert parsed["base_ref"] == "origin/main"
        assert parsed["agents"]["default"] == "claude"
        assert parsed["agents"]["review_default"] == "codex"
        assert parsed["agents"]["allowed"] == ["claude", "codex"]
        assert parsed["retry"]["cap"] == 20
        assert parsed["implement"]["setup_command"] == "scripts/implement-setup.sh"
        assert parsed["implement"]["teardown_command"] == "scripts/implement-teardown.sh"
        assert len(parsed["verify"]["gates"]) == 1
        assert parsed["verify"]["gates"][0]["name"] == "test"

    def test_generates_comments_when_no_gates(self):
        content = _generate_spec_toml(
            base_ref="origin/main",
            default_agent="claude",
            review_default="claude",
            allowed_agents=["claude"],
            gates=[],
        )
        assert "# No verify gates were auto-detected" in content
        assert "[[verify.gates]]" not in content.replace("# [[verify.gates]]", "")

    def test_escapes_quotes_in_commands(self):
        """Commands with double quotes must produce valid TOML (F2)."""
        import tomllib

        content = _generate_spec_toml(
            base_ref="origin/main",
            default_agent="claude",
            review_default="claude",
            allowed_agents=["claude"],
            gates=[
                {
                    "name": "test",
                    "command": 'bash -lc "pytest -q"',
                    "parallel": False,
                }
            ],
            install_command='bash -lc "pip install -e ."',
        )
        parsed = tomllib.loads(content)
        assert parsed["bootstrap"]["install_command"] == 'bash -lc "pip install -e ."'
        assert parsed["verify"]["gates"][0]["command"] == 'bash -lc "pytest -q"'

    def test_generated_windows_bootstrap_roundtrips_through_config_loader(self, tmp_path: Path):
        from spec_runtime.config import load_repo_spec_runtime_config

        content = _generate_spec_toml(
            base_ref="origin/main",
            default_agent="claude",
            review_default="claude",
            allowed_agents=["claude"],
            gates=[],
            install_command="python -m pip install -e .",
        )
        (tmp_path / ".spec.toml").write_text(content)

        config = load_repo_spec_runtime_config(tmp_path, require=True)
        selected = config.bootstrap_install.select(windows=True)
        assert selected is not None
        assert selected.shell == "powershell"
        assert r".\.venv\Scripts\python.exe" in str(selected.value)

    def test_generated_python_commands_preserve_windows_dev_install_and_gates(
        self,
        tmp_path: Path,
    ):
        from spec_runtime.config import load_repo_spec_runtime_config

        content = _generate_spec_toml(
            base_ref="origin/main",
            default_agent="codex",
            review_default="codex",
            allowed_agents=["codex"],
            gates=[
                {
                    "name": "test",
                    "command": ".venv/bin/python -m pytest",
                    "argv_windows": [
                        ".venv/Scripts/python.exe",
                        "-m",
                        "pytest",
                    ],
                    "parallel": True,
                }
            ],
            install_command=(
                "python -m venv .venv && "
                ".venv/bin/python -m pip install -e '.[dev]'"
            ),
        )
        (tmp_path / ".spec.toml").write_text(content)

        config = load_repo_spec_runtime_config(tmp_path, require=True)
        install = config.bootstrap_install.select(windows=True)
        gate = config.verify_gates[0].command_variants.select(windows=True)

        assert install is not None
        assert "'.[dev]'" in str(install.value)
        assert gate is not None
        assert gate.argv(windows=True) == [
            ".venv/Scripts/python.exe",
            "-m",
            "pytest",
        ]

    def test_escapes_backslashes_in_commands(self):
        """Backslashes must be escaped for valid TOML (F2)."""
        import tomllib

        content = _generate_spec_toml(
            base_ref="origin/main",
            default_agent="claude",
            review_default="claude",
            allowed_agents=["claude"],
            gates=[],
            setup_command="C:\\Users\\ci\\setup.bat",
        )
        parsed = tomllib.loads(content)
        assert parsed["implement"]["setup_command"] == "C:\\Users\\ci\\setup.bat"


class TestTomlEscape:
    def test_escapes_double_quotes(self):
        assert _toml_escape('say "hello"') == 'say \\"hello\\"'

    def test_escapes_backslashes(self):
        assert _toml_escape("a\\b") == "a\\\\b"

    def test_escapes_newlines(self):
        assert _toml_escape("line1\nline2") == "line1\\nline2"

    def test_no_op_on_plain_string(self):
        assert _toml_escape("plain text") == "plain text"


# ---------------------------------------------------------------------------
# Gitignore update
# ---------------------------------------------------------------------------


class TestUpdateGitignore:
    def test_creates_gitignore_if_missing(self, tmp_path):
        assert _update_gitignore(tmp_path) is True
        content = (tmp_path / ".gitignore").read_text()
        assert ".spec-state/" in content
        assert ".spec-workspaces/" in content
        assert ".worktrees/" in content
        assert ".venv/" in content

    def test_appends_missing_entries(self, tmp_path):
        (tmp_path / ".gitignore").write_text("node_modules/\n")
        assert _update_gitignore(tmp_path) is True
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert ".spec-state/" in content

    def test_does_not_duplicate(self, tmp_path):
        (tmp_path / ".gitignore").write_text(
            ".venv/\n"
            ".spec-state/\n"
            ".spec-workspaces/\n"
            ".worktrees/\n"
            ".spec.local.toml\n"
            ".claude/settings.local.json\n"
            ".claude/mcp-servers.json\n"
            ".codex/\n"
            ".spec-claude-home/\n"
            ".spec-codex-home/\n"
        )
        assert _update_gitignore(tmp_path) is False

    def test_ignores_agent_homes_and_claude_mcp(self, tmp_path):
        """Runtime files that may contain user credentials must be ignored so
        the agent's MCP allowlist passthrough (.claude/mcp-servers.json) and
        the isolated agent homes' copied auth files are never accidentally
        committed."""
        assert _update_gitignore(tmp_path) is True
        content = (tmp_path / ".gitignore").read_text()
        assert ".spec-claude-home/" in content
        assert ".spec-codex-home/" in content
        assert ".claude/mcp-servers.json" in content


# ---------------------------------------------------------------------------
# Full init command
# ---------------------------------------------------------------------------


class TestCmdInit:
    _patch_root = "spec_runtime.init._git_repo_root"
    _patch_agents = "spec_runtime.init._detect_agents"

    def _mock_agents(self):
        return patch(
            self._patch_agents,
            return_value=("claude", ["claude"]),
        )

    def test_creates_all_artifacts(self, tmp_path, capsys):
        _init_git_repo(tmp_path)
        args = MagicMock(force=False, yolo=False)
        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
        ):
            rc = cmd_init(args)
        assert rc == 0
        assert (tmp_path / ".spec.toml").is_file()
        assert (tmp_path / "specs").is_dir()
        assert (tmp_path / "specs" / "tasks").is_dir()
        # Marker file so the empty tasks dir survives `git add specs/` + clone.
        assert (tmp_path / "specs" / "tasks" / ".gitkeep").is_file()
        assert (tmp_path / ".github" / "prompts" / "review.md").is_file()
        assert (tmp_path / "AGENTS.md").is_file()
        assert "spec doctor" in capsys.readouterr().out

    def test_template_copy_declares_utf8_encoding(self, tmp_path):
        """Do not fall back to the native Windows ANSI code page."""
        with patch.object(Path, "write_text", autospec=True) as write_text:
            assert _copy_template(
                tmp_path,
                "review.md",
                ".github/prompts/review.md",
                force=False,
            )

        assert write_text.call_args.kwargs["encoding"] == "utf-8"

    def test_refuses_without_git(self):
        args = MagicMock(force=False, yolo=False)
        with patch(self._patch_root, return_value=None):
            rc = cmd_init(args)
        assert rc == 1

    def test_refuses_existing_config_without_force(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / ".spec.toml").write_text("existing")
        args = MagicMock(force=False, yolo=False)
        with patch(self._patch_root, return_value=tmp_path):
            rc = cmd_init(args)
        assert rc == 1
        assert (tmp_path / ".spec.toml").read_text() == "existing"

    def test_fails_when_no_agents_on_path(self, tmp_path, capsys):
        _init_git_repo(tmp_path)
        args = MagicMock(force=False, yolo=False)
        with (
            patch(self._patch_root, return_value=tmp_path),
            patch(self._patch_agents, return_value=("", [])),
        ):
            rc = cmd_init(args)
        assert rc == 1
        out = capsys.readouterr().out
        assert "No coding agents found on PATH" in out
        assert "claude, codex" in out

    def test_force_preserves_config_but_refreshes_templates(
        self,
        tmp_path,
    ):
        _init_git_repo(tmp_path)
        (tmp_path / ".spec.toml").write_text("existing-config")
        (tmp_path / "AGENTS.md").write_text("old agents")
        args = MagicMock(force=True, yolo=False)
        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
        ):
            rc = cmd_init(args)
        assert rc == 0
        assert (tmp_path / ".spec.toml").read_text() == "existing-config"
        assert (tmp_path / "AGENTS.md").read_text() != "old agents"

    def test_generated_config_is_loadable(self, tmp_path):
        _init_git_repo(tmp_path)
        args = MagicMock(force=False, yolo=False)
        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
        ):
            cmd_init(args)

        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)
        assert "main" in config.base_ref or "master" in config.base_ref
        assert config.agents.default in ("claude", "codex")
        assert config.agents.review_default in ("claude", "codex")
        assert config.retry_cap == 20

    def test_init_prefers_codex_for_reviews_when_available(self, tmp_path):
        _init_git_repo(tmp_path)
        args = MagicMock(force=False)
        with (
            patch(self._patch_root, return_value=tmp_path),
            patch(self._patch_agents, return_value=("claude", ["claude", "codex"])),
        ):
            rc = cmd_init(args)

        assert rc == 0
        with patch("spec_runtime.config._config_path", return_value=tmp_path / ".spec.toml"):
            parsed = load_spec_runtime_config(require=True)
        assert parsed.agents.default == "claude"
        assert parsed.agents.review_default == "codex"


# ---------------------------------------------------------------------------
# Agent merge command building
# ---------------------------------------------------------------------------


class TestBuildAgentMergeCommand:
    def test_claude_command(self):
        cmd = _build_agent_merge_command("claude")
        assert cmd == ["claude", "-p"]

    def test_codex_command(self):
        cmd = _build_agent_merge_command("codex")
        assert cmd == ["codex", "exec"]

    def test_unknown_agent(self):
        cmd = _build_agent_merge_command("unknown")
        assert cmd == []


# ---------------------------------------------------------------------------
# Agent merge invocation
# ---------------------------------------------------------------------------


class TestMergeFileWithAgent:
    _fn = "AGENTS.md"
    _patch_run = "spec_runtime.init.subprocess.run"

    def test_successful_merge(self):
        fake = MagicMock(returncode=0, stdout="merged content")
        with patch(self._patch_run, return_value=fake) as m:
            result = _merge_file_with_agent(
                "claude",
                "existing",
                "template",
                self._fn,
            )
        assert result == "merged content"
        m.assert_called_once()
        cmd = m.call_args[0][0]
        assert cmd == ["claude", "-p"]
        # Prompt passed via stdin for claude
        assert m.call_args[1]["input"] is not None
        assert "EXISTING" in m.call_args[1]["input"]

    def test_codex_passes_prompt_via_stdin(self):
        fake = MagicMock(returncode=0, stdout="merged content")
        with patch(self._patch_run, return_value=fake) as m:
            result = _merge_file_with_agent(
                "codex",
                "existing",
                "template",
                self._fn,
            )
        assert result == "merged content"
        m.assert_called_once()
        cmd = m.call_args[0][0]
        # Codex: prompt sent via stdin to avoid OS argv limits
        assert cmd == ["codex", "exec"]
        assert m.call_args[1]["input"] is not None
        assert "EXISTING" in m.call_args[1]["input"]

    def test_agent_failure_returns_none(self):
        fake = MagicMock(returncode=1, stdout="", stderr="err")
        with patch(self._patch_run, return_value=fake):
            result = _merge_file_with_agent(
                "claude",
                "existing",
                "template",
                self._fn,
            )
        assert result is None

    def test_empty_stdout_returns_none(self):
        """Agent exits 0 but produces no output — treat as failure."""
        fake = MagicMock(returncode=0, stdout="")
        with patch(self._patch_run, return_value=fake):
            result = _merge_file_with_agent(
                "claude",
                "existing",
                "template",
                self._fn,
            )
        assert result is None

    def test_whitespace_only_stdout_returns_none(self):
        """Agent exits 0 but stdout is whitespace-only — treat as failure."""
        fake = MagicMock(returncode=0, stdout="  \n  ")
        with patch(self._patch_run, return_value=fake):
            result = _merge_file_with_agent(
                "claude",
                "existing",
                "template",
                self._fn,
            )
        assert result is None

    def test_timeout_returns_none(self):
        err = subprocess.TimeoutExpired("cmd", 60)
        with patch(self._patch_run, side_effect=err):
            result = _merge_file_with_agent(
                "claude",
                "existing",
                "template",
                self._fn,
            )
        assert result is None

    def test_os_error_returns_none(self):
        with patch(self._patch_run, side_effect=OSError):
            result = _merge_file_with_agent(
                "claude",
                "existing",
                "template",
                self._fn,
            )
        assert result is None

    def test_unknown_agent_returns_none(self):
        result = _merge_file_with_agent(
            "unknown",
            "existing",
            "template",
            self._fn,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Auto-merge in cmd_init
# ---------------------------------------------------------------------------


class TestCmdInitAutoMerge:
    _patch_merge = "spec_runtime.init._merge_file_with_agent"
    _patch_agents = "spec_runtime.init._detect_agents"
    _patch_root = "spec_runtime.init._git_repo_root"

    def _mock_agents(self):
        return patch(
            self._patch_agents,
            return_value=("claude", ["claude"]),
        )

    def test_merge_accepted(self, tmp_path, capsys):
        """User accepts merge: agent invoked, file updated."""
        _init_git_repo(tmp_path)
        (tmp_path / "AGENTS.md").write_text("existing content")
        args = MagicMock(force=False, yolo=False)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch("builtins.input", return_value="y"),
            patch(
                self._patch_merge,
                return_value="merged content",
            ) as mock_merge,
        ):
            rc = cmd_init(args)

        assert rc == 0
        assert (tmp_path / "AGENTS.md").read_text() == "merged content"
        mock_merge.assert_called_once()
        assert mock_merge.call_args[0][0] == "claude"
        out = capsys.readouterr().out
        assert "Merged AGENTS.md" in out
        assert "Agent merged:" in out

    def test_merge_declined(self, tmp_path, capsys):
        """User declines: file unchanged, manual hint shown."""
        _init_git_repo(tmp_path)
        (tmp_path / "AGENTS.md").write_text("existing content")
        args = MagicMock(force=False, yolo=False)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch("builtins.input", return_value="n"),
        ):
            rc = cmd_init(args)

        assert rc == 0
        assert (tmp_path / "AGENTS.md").read_text() == "existing content"
        out = capsys.readouterr().out
        assert "Skipped AGENTS.md" in out
        # Declined file should not be in git add
        lines = out.splitlines()
        git_add = [x for x in lines if "git add" in x]
        assert git_add
        assert "AGENTS.md" not in git_add[0]

    def test_merge_agent_failure(self, tmp_path, capsys):
        """Agent failure: file unchanged, warning shown."""
        _init_git_repo(tmp_path)
        (tmp_path / "AGENTS.md").write_text("existing content")
        args = MagicMock(force=False, yolo=False)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch("builtins.input", return_value="y"),
            patch(self._patch_merge, return_value=None),
        ):
            rc = cmd_init(args)

        assert rc == 0
        assert (tmp_path / "AGENTS.md").read_text() == "existing content"
        out = capsys.readouterr().out
        assert "Agent failed to merge AGENTS.md" in out

    def test_merge_default_enter_accepts(self, tmp_path):
        """Pressing Enter (empty) should accept merge."""
        _init_git_repo(tmp_path)
        (tmp_path / "AGENTS.md").write_text("existing content")
        args = MagicMock(force=False, yolo=False)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch("builtins.input", return_value=""),
            patch(
                self._patch_merge,
                return_value="merged content",
            ),
        ):
            rc = cmd_init(args)

        assert rc == 0
        assert (tmp_path / "AGENTS.md").read_text() == "merged content"

    def test_eof_declines_merge(self, tmp_path):
        """EOFError (non-interactive) should decline merge."""
        _init_git_repo(tmp_path)
        (tmp_path / "AGENTS.md").write_text("existing content")
        args = MagicMock(force=False, yolo=False)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch("builtins.input", side_effect=EOFError),
        ):
            rc = cmd_init(args)

        assert rc == 0
        assert (tmp_path / "AGENTS.md").read_text() == "existing content"

    def test_git_add_includes_merged_excludes_declined(
        self,
        tmp_path,
        capsys,
    ):
        """git add includes merged, excludes declined."""
        _init_git_repo(tmp_path)
        (tmp_path / "AGENTS.md").write_text("existing agents")
        (tmp_path / "CLAUDE.md").write_text("existing claude")
        args = MagicMock(force=False, yolo=False)

        # Accept AGENTS.md, decline CLAUDE.md
        inputs = iter(["y", "n"])
        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch(
                "builtins.input",
                side_effect=lambda _: next(inputs),
            ),
            patch(
                self._patch_merge,
                return_value="merged agents",
            ),
        ):
            rc = cmd_init(args)

        assert rc == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        git_add = [x for x in lines if "git add" in x]
        assert git_add
        assert "AGENTS.md" in git_add[0]
        assert "CLAUDE.md" not in git_add[0]

    def test_declined_review_md_excluded_from_git_add(
        self,
        tmp_path,
        capsys,
    ):
        """Declined .github/prompts/review.md excluded from git add."""
        _init_git_repo(tmp_path)
        review_dir = tmp_path / ".github" / "prompts"
        review_dir.mkdir(parents=True)
        (review_dir / "review.md").write_text("existing review")
        args = MagicMock(force=False, yolo=False)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch("builtins.input", return_value="n"),
        ):
            rc = cmd_init(args)

        assert rc == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        git_add = [x for x in lines if "git add" in x]
        assert git_add
        assert ".github/prompts/" not in git_add[0]


# ---------------------------------------------------------------------------
# Yolo detection (agent-assisted config)
# ---------------------------------------------------------------------------


class TestYoloDetection:
    _patch_root = "spec_runtime.init._git_repo_root"
    _patch_agents = "spec_runtime.init._detect_agents"
    _patch_ask = "spec_runtime.init._ask_agent_for_config"
    _patch_run = "spec_runtime.init.subprocess.run"

    def _mock_agents(self):
        return patch(
            self._patch_agents,
            return_value=("claude", ["claude"]),
        )

    # --- _gather_repo_context ---

    def test_gather_repo_context_includes_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project\nSome description\n")
        ctx = _gather_repo_context(tmp_path)
        assert "# My Project" in ctx
        assert "Some description" in ctx

    def test_gather_repo_context_truncates(self, tmp_path):
        lines = [f"line {i}" for i in range(200)]
        (tmp_path / "README.md").write_text("\n".join(lines))
        ctx = _gather_repo_context(tmp_path)
        assert "line 99" in ctx
        assert "line 100" not in ctx

    def test_gather_repo_context_includes_ci_config(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: CI\non: push\n")
        ctx = _gather_repo_context(tmp_path)
        assert "name: CI" in ctx
        assert ".github/workflows/ci.yml" in ctx

    def test_gather_repo_context_includes_yaml_workflows(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "deploy.yaml").write_text("name: Deploy\non: push\n")
        ctx = _gather_repo_context(tmp_path)
        assert "name: Deploy" in ctx
        assert ".github/workflows/deploy.yaml" in ctx

    def test_gather_repo_context_normalizes_windows_ci_heading(
        self,
        tmp_path,
        monkeypatch,
    ):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: CI\n")
        concrete_path = type(tmp_path)
        real_relative_to = concrete_path.relative_to

        def windows_relative_to(path, other, *args, **kwargs):
            relative = real_relative_to(path, other, *args, **kwargs)
            return PureWindowsPath(*relative.parts)

        monkeypatch.setattr(concrete_path, "relative_to", windows_relative_to)

        ctx = _gather_repo_context(tmp_path)

        assert "--- .github/workflows/ci.yml ---" in ctx
        assert r".github\workflows\ci.yml" not in ctx

    def test_gather_repo_context_includes_build_manifests(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py311"\n')
        (tmp_path / "package.json").write_text('{"name": "test"}\n')
        ctx = _gather_repo_context(tmp_path)
        assert "pyproject.toml" in ctx
        assert "package.json" in ctx
        assert "tool.ruff" in ctx

    # --- _build_yolo_prompt ---

    def test_build_yolo_prompt_includes_example_spec(self):
        prompt = _build_yolo_prompt("some repo context")
        # The example spec.toml contains base_ref and [verify] sections
        assert "base_ref" in prompt
        assert "cap = 20" in prompt
        assert "[verify]" in prompt
        assert "install_command" in prompt

    def test_build_yolo_prompt_includes_repo_context(self):
        prompt = _build_yolo_prompt("UNIQUE_CONTEXT_STRING_12345")
        assert "UNIQUE_CONTEXT_STRING_12345" in prompt

    # --- _ask_agent_for_config ---

    def test_ask_agent_for_config_parses_json(self):
        valid_json = json.dumps(
            {
                "install_command": "pip install -e .",
                "gates": [{"name": "test", "command": "pytest", "parallel": True}],
                "setup_command": "",
                "teardown_command": "",
            }
        )
        fake = MagicMock(returncode=0, stdout=valid_json)
        with patch(self._patch_run, return_value=fake):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is not None
        assert result["install_command"] == "pip install -e ."
        assert len(result["gates"]) == 1
        assert result["gates"][0]["name"] == "test"

    def test_ask_agent_for_config_strips_markdown_fences(self):
        inner = json.dumps(
            {
                "install_command": "npm ci",
                "gates": [],
                "setup_command": "",
                "teardown_command": "",
            }
        )
        fenced = f"```json\n{inner}\n```"
        fake = MagicMock(returncode=0, stdout=fenced)
        with patch(self._patch_run, return_value=fake):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is not None
        assert result["install_command"] == "npm ci"

    def test_ask_agent_for_config_returns_none_on_timeout(self):
        with patch(
            self._patch_run,
            side_effect=subprocess.TimeoutExpired("cmd", 120),
        ):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is None

    def test_ask_agent_for_config_returns_none_on_bad_json(self):
        fake = MagicMock(returncode=0, stdout="this is not json")
        with patch(self._patch_run, return_value=fake):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is None

    def test_ask_agent_for_config_returns_none_on_missing_keys(self):
        incomplete = json.dumps({"install_command": "pip install -e ."})
        fake = MagicMock(returncode=0, stdout=incomplete)
        with patch(self._patch_run, return_value=fake):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is None

    def test_ask_agent_for_config_rejects_wrong_gate_types(self):
        """parallel as string 'false' should be rejected (F3)."""
        bad = json.dumps(
            {
                "install_command": "pip install",
                "gates": [{"name": "test", "command": "pytest", "parallel": "false"}],
                "setup_command": "",
                "teardown_command": "",
            }
        )
        fake = MagicMock(returncode=0, stdout=bad)
        with patch(self._patch_run, return_value=fake):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is None

    def test_ask_agent_for_config_rejects_non_string_command(self):
        bad = json.dumps(
            {
                "install_command": 123,
                "gates": [],
                "setup_command": "",
                "teardown_command": "",
            }
        )
        fake = MagicMock(returncode=0, stdout=bad)
        with patch(self._patch_run, return_value=fake):
            result = _ask_agent_for_config("claude", "prompt")
        assert result is None

    def test_ask_agent_for_config_codex_passes_prompt_via_stdin(self):
        """Codex should receive prompt via stdin to avoid OS argv limits."""
        valid_json = json.dumps(
            {
                "install_command": "pip install -e .",
                "gates": [{"name": "test", "command": "pytest", "parallel": True}],
                "setup_command": "",
                "teardown_command": "",
            }
        )
        fake = MagicMock(returncode=0, stdout=valid_json)
        with patch(self._patch_run, return_value=fake) as m:
            result = _ask_agent_for_config("codex", "my prompt")
        assert result is not None
        cmd = m.call_args[0][0]
        assert cmd == ["codex", "exec"]
        assert m.call_args[1]["input"] == "my prompt"

    # --- cmd_init with --yolo ---

    def test_yolo_flag_invokes_agent(self, tmp_path):
        _init_git_repo(tmp_path)
        agent_result = {
            "install_command": "make install",
            "gates": [{"name": "test", "command": "make test", "parallel": True}],
            "setup_command": "",
            "teardown_command": "",
        }
        args = MagicMock(force=False, yolo=True)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch(self._patch_ask, return_value=agent_result) as mock_ask,
            patch("spec_runtime.init._detect_verify_gates") as mock_gates,
            patch("spec_runtime.init._detect_install_command") as mock_install,
            patch("spec_runtime.init._detect_implement_commands") as mock_impl,
        ):
            rc = cmd_init(args)

        assert rc == 0
        mock_ask.assert_called_once()
        mock_gates.assert_not_called()
        mock_install.assert_not_called()
        mock_impl.assert_not_called()
        # Verify agent values were used in the generated config
        content = (tmp_path / ".spec.toml").read_text()
        assert "make install" in content
        assert "make test" in content

    def test_yolo_errors_on_agent_failure(self, tmp_path):
        _init_git_repo(tmp_path)
        args = MagicMock(force=False, yolo=True)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch(self._patch_ask, return_value=None),
        ):
            rc = cmd_init(args)

        assert rc == 1
        # Config should not be created on failure
        assert not (tmp_path / ".spec.toml").exists()

    def test_yolo_skipped_when_config_exists(self, tmp_path):
        """--force --yolo preserves config without invoking the agent."""
        _init_git_repo(tmp_path)
        (tmp_path / ".spec.toml").write_text("# existing config\n")
        args = MagicMock(force=True, yolo=True)

        with (
            patch(self._patch_root, return_value=tmp_path),
            self._mock_agents(),
            patch(self._patch_ask) as mock_ask,
        ):
            rc = cmd_init(args)

        assert rc == 0
        mock_ask.assert_not_called()
        # Original config content is preserved
        assert (tmp_path / ".spec.toml").read_text() == "# existing config\n"

    def test_cli_parses_yolo_flag(self):
        from spec_runtime.cli import main

        with (
            patch("spec_runtime.init._git_repo_root", return_value=None),
            patch("spec_runtime.cli._cmd_init") as mock_init,
        ):
            mock_init.return_value = 0
            main(["init", "--yolo"])

        call_args = mock_init.call_args[0][0]
        assert call_args.yolo is True


# ---------------------------------------------------------------------------
# Bundled AGENTS.md template — prompt wording regression
# ---------------------------------------------------------------------------


class TestBundledAgentsTemplate:
    def test_template_does_not_forbid_spec_report(self):
        content = _read_bundled_template("AGENTS.md")
        ban_lines = [line for line in content.splitlines() if "Do not run orchestrator" in line]
        assert ban_lines, "Expected a 'Do not run orchestrator' line in template"
        ban_clause = ban_lines[0].split("The only orchestrator command")[0]
        assert "spec report" not in ban_clause


def test_detect_verify_gates_swift_package(tmp_path):
    """Package.swift projects get Swift build and test gates."""
    from spec_runtime.init import _detect_verify_gates

    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")
    gates = _detect_verify_gates(tmp_path)
    by_name = {g["name"]: g["command"] for g in gates}
    assert by_name.get("build") == "swift build"
    assert by_name.get("test") == "swift test"


def test_detect_verify_gates_makefile_wins_over_swift(tmp_path):
    from spec_runtime.init import _detect_verify_gates

    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")
    (tmp_path / "Makefile").write_text("test:\n\tswift test\n")
    gates = _detect_verify_gates(tmp_path)
    by_name = {g["name"]: g["command"] for g in gates}
    assert by_name["test"] == "make test"
    assert by_name["build"] == "swift build"
