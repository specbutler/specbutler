"""Run Windows-only tests' fixtures through the product on every platform.

Windows-only tests skip on the macOS and Linux machines where implement agents
and host verify run.  A fixture such a test owns (a spec file, a fake provider
CLI) can then fall behind a product change and fail only on the windows-probe
CI job, a full review and CI round later.  These tests feed the same fixtures
through the same product checks here, so the drift shows up locally.

The suite-wide Windows emulation (cp1252 default text encoding, a backslash in
every ``tmp_path``) lives in ``tests/conftest.py``; the first test below keeps
it honest.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import test_windows_probe

from spec_runtime.spec_lint import lint_specs

TESTS_DIR = Path(__file__).resolve().parent

_SPEC_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


@pytest.mark.skipif(os.name == "nt", reason="Windows already uses cp1252 natively")
def test_posix_runs_default_text_encoding_like_windows(tmp_path: Path) -> None:
    assert "\\" in str(tmp_path)
    path = tmp_path / "snow.txt"
    path.write_bytes("snow 雪\n".encode("utf-8"))
    assert path.read_text() == "snow é›ª\n"
    assert path.read_text(encoding="utf-8") == "snow 雪\n"
    with pytest.raises(UnicodeEncodeError):
        path.write_text("snow 雪\n")


def _is_windows_only_condition(node: ast.expr) -> bool:
    """``sys.platform != "win32"`` or ``os.name != "nt"``."""
    return (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.NotEq)
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value in {"win32", "nt"}
    )


def _is_windows_only_skip(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skipif"
        and bool(node.args)
        and _is_windows_only_condition(node.args[0])
    )


def _windows_only_scopes(tree: ast.Module) -> list[ast.AST]:
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in statement.targets)
            and _is_windows_only_skip(statement.value)
        ):
            return [tree]
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and any(_is_windows_only_skip(decorator) for decorator in node.decorator_list)
    ]


def _windows_only_fixture_specs() -> list[tuple[str, str, str]]:
    """Return ``(location, spec id, text)`` for spec literals in Windows-only tests."""
    found: dict[tuple[str, int], tuple[str, str, str]] = {}
    for module in sorted(TESTS_DIR.glob("test_*.py")):
        source = module.read_text(encoding="utf-8")
        if "win32" not in source and '"nt"' not in source:
            continue
        for scope in _windows_only_scopes(ast.parse(source)):
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                text = node.value
                if not text.startswith("---\n"):
                    continue
                frontmatter = text.split("\n---\n", 1)[0]
                match = _SPEC_ID_RE.search(frontmatter)
                if match:
                    location = f"{module.name}:{node.lineno}"
                    found[(module.name, node.lineno)] = (location, match.group(1), text)
    return list(found.values())


def test_windows_only_fixture_specs_pass_spec_lint(tmp_path: Path) -> None:
    """Fixture specs the Windows CLI matrix dispatches must survive ``spec lint``.

    ``spec implement`` and ``spec auto run`` refuse specs with lint errors, so a
    Windows-only fixture spec that a new lint rule rejects breaks only the
    windows-probe job unless it is linted here.
    """
    fixtures = _windows_only_fixture_specs()
    assert {"windows-cli-flow", "windows-auto-ready", "unicode-content"} <= {
        spec_id for _, spec_id, _ in fixtures
    }, "the fixture scan no longer finds test_windows_probe.py's specs"
    failures = []
    for index, (location, spec_id, text) in enumerate(fixtures):
        repo = tmp_path / f"fixture-{index}"
        (repo / "specs").mkdir(parents=True)
        (repo / "specs" / f"{spec_id}.md").write_text(text, encoding="utf-8")
        for result in lint_specs(repo, merged_spec_ids=set()):
            failures.extend(f"{location} ({result.spec_id}): {error}" for error in result.errors)
    assert not failures, "\n".join(failures)


def _write_posix_fake_cli_tools(fake_bin: Path) -> None:
    """Install test_windows_probe's fake ``gh`` and ``codex`` as POSIX scripts."""
    python = sys.executable
    fake_bin.mkdir()
    codex_source = test_windows_probe.FAKE_CODEX_SOURCE.replace("__FIXTURE_PYTHON__", repr(python))
    scripts = {
        "gh": test_windows_probe.FAKE_GH_SOURCE,
        "codex": codex_source.split("\n", 1)[1],
    }
    for name, source in scripts.items():
        script = fake_bin / name
        script.write_text(f"#!{python} -I\n{source}", encoding="utf-8")
        script.chmod(0o755)
    git = shutil.which("git")
    assert git
    (fake_bin / "git").symlink_to(git)


@pytest.mark.skipif(os.name == "nt", reason="test_windows_probe runs the real Windows launchers")
def test_windows_probe_fake_clis_satisfy_doctor(tmp_path: Path) -> None:
    """Mirror ``test_spec_init_output_is_accepted_by_doctor`` with POSIX launchers.

    When doctor gains a provider probe (``codex exec resume --help``, for
    example), the Windows probe's fake ``codex`` must answer it or that test's
    "0 warning(s)" assertion fails on windows-probe.  The fakes are the same
    Python source here, so the gap fails on every platform.
    """
    fake_bin = tmp_path / "fake-bin"
    _write_posix_fake_cli_tools(fake_bin)
    env = os.environ.copy()
    env.pop("SPEC_CONFIG", None)
    env["PATH"] = str(fake_bin)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("probe\n", encoding="utf-8")

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv), cwd=repo, env=env, text=True, encoding="utf-8", capture_output=True, check=False
        )

    for argv in (
        ("git", "init", "-b", "main"),
        ("git", "add", "README.md"),
        ("git", "-c", "user.name=Parity", "-c", "user.email=parity@example.invalid", "commit", "-m", "probe"),
        ("git", "remote", "add", "origin", "https://github.com/example/spec-windows-probe.git"),
        ("git", "update-ref", "refs/remotes/origin/main", "HEAD"),
    ):
        completed = run(*argv)
        assert completed.returncode == 0, completed.stdout + completed.stderr

    initialized = run(sys.executable, "-m", "spec_runtime.cli", "init")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    config_text = (repo / ".spec.toml").read_text(encoding="utf-8")
    assert 'allowed = ["codex"]' in config_text
    doctor = run(sys.executable, "-m", "spec_runtime.cli", "doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "0 warning(s)" in doctor.stdout, doctor.stdout
    assert "codex-cli 0.156.1" in doctor.stdout
