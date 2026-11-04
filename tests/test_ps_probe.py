"""The conftest ``ps`` probe that gates ``requires_ps`` tests."""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import probe_ps, ps_unavailability_reason

posix_only = pytest.mark.skipif(os.name != "posix", reason="fake ps is a POSIX shell script")

pytest_plugins = ["pytester"]


def _write_fake_ps(fake_bin: Path, body: str) -> None:
    """Install a ``ps`` double at the external CLI boundary."""
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / "ps"
    script.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o755)


def _path_env(fake_bin: Path) -> dict[str, str]:
    return {**os.environ, "PATH": str(fake_bin)}


@posix_only
def test_probe_accepts_a_ps_that_inspects_this_process(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    _write_fake_ps(fake_bin, '[ "$1" = "-p" ] || exit 2\necho "  PID TTY TIME CMD"\necho "$2 ?? 0:00.01 python"\n')

    assert probe_ps(_path_env(fake_bin)) is None


@posix_only
def test_probe_reports_a_missing_ps(tmp_path: Path) -> None:
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()

    assert probe_ps(_path_env(empty_bin)) == "ps not found"


@posix_only
def test_probe_reports_a_permission_denied_ps(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    _write_fake_ps(fake_bin, 'echo "ps: Operation not permitted" >&2\nexit 1\n')

    assert probe_ps(_path_env(fake_bin)) == "Operation not permitted"


@posix_only
def test_probe_reports_other_ps_failures_with_their_exit_status(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    _write_fake_ps(fake_bin, 'echo "ps: no such process" >&2\nexit 1\n')

    assert probe_ps(_path_env(fake_bin)) == "ps exited 1: ps: no such process"


def test_non_posix_never_gates_on_ps(tmp_path: Path) -> None:
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()

    assert ps_unavailability_reason("nt", _path_env(empty_bin)) is None
    if os.name == "posix":
        assert ps_unavailability_reason("posix", _path_env(empty_bin)) == "ps not found"


def _run_marked_suite(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fake_bin: Path | None = None
) -> pytest.RunResult:
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    pytester.makeini("[pytest]\nmarkers =\n    requires_ps: needs a working ps\n")
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.requires_ps
        def test_needs_ps_one():
            pass

        @pytest.mark.requires_ps
        def test_needs_ps_two():
            pass

        def test_does_not_need_ps():
            pass
        """
    )
    if fake_bin is not None:
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    return pytester.runpytest_subprocess("-rs", "-p", "no:cacheprovider")


@posix_only
def test_unusable_ps_skips_marked_tests_with_one_summary_line(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    _write_fake_ps(fake_bin, 'echo "ps: Operation not permitted" >&2\nexit 1\n')

    result = _run_marked_suite(pytester, monkeypatch, fake_bin)

    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*ps unavailable (Operation not permitted)*"])
    summary = [line for line in result.outlines if line.startswith("2 tests skipped: ps unavailable")]
    assert summary == ["2 tests skipped: ps unavailable (Operation not permitted)"]


@posix_only
def test_usable_ps_skips_nothing_and_prints_no_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    _write_fake_ps(fake_bin, f'exec "{sys.executable}" -c "print(\'PID CMD\')"\n')

    result = _run_marked_suite(pytester, monkeypatch, fake_bin)

    result.assert_outcomes(passed=3)
    assert not any("ps unavailable" in line for line in result.outlines)


@pytest.mark.skipif(os.name == "posix", reason="exercises the non-POSIX gate on its real platform")
def test_non_posix_runs_requires_ps_tests_without_ps(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_marked_suite(pytester, monkeypatch)

    result.assert_outcomes(passed=3)
    assert not any("ps unavailable" in line for line in result.outlines)


def test_user_state_is_isolated_and_inherited_by_children() -> None:
    import json
    import subprocess

    from spec_runtime.provider_env import specbutler_user_state_root

    root = specbutler_user_state_root()
    assert root.parent == Path(os.environ["XDG_STATE_HOME"])
    assert Path.home().is_relative_to(root.parent.parent)
    child = subprocess.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(child.stdout)["XDG_STATE_HOME"] == str(root.parent)


def test_network_probe_reports_permission_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    import conftest

    def denied():
        raise PermissionError("network denied")

    monkeypatch.setattr(conftest.socket, "socket", denied)
    assert conftest.probe_network() == "network denied"


def test_nested_sandbox_probe_reports_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import conftest

    monkeypatch.setattr(conftest.shutil, "which", lambda _: None)
    assert conftest.probe_nested_sandbox() == "Codex CLI is not installed"


def test_provider_home_probe_reports_denial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import conftest

    for key in conftest._PROVIDER_AUTH_ENV_KEYS["codex"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(conftest._REAL_PROVIDER_HOMES, "codex", tmp_path / "missing")
    assert conftest.probe_provider_home("codex") is not None
    monkeypatch.setitem(conftest._REAL_PROVIDER_HOMES, "codex", tmp_path)
    assert conftest.probe_provider_home("codex") is None


@pytest.mark.parametrize("available", [False, True])
def test_capabilities_preserve_coverage_and_summarize_skips(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, available: bool,
) -> None:
    # CI with Chrome installed fails browser tests instead; see the test below.
    monkeypatch.delenv("CI", raising=False)
    source = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    reason = "None" if available else repr("denied by test sandbox")
    pytester.makeconftest(source + f"""
def probe_nested_sandbox():
    return {reason}
def probe_network():
    return {reason}
def probe_browser():
    return {reason}
def probe_provider_home(provider):
    return {reason}
""")
    pytester.makeini("""[pytest]
markers =
    requires_nested_sandbox: nested sandbox
    requires_network: networking
    requires_browser: headless browser
    requires_provider_home: provider home
""")
    pytester.makepyfile("""
import pytest
@pytest.mark.requires_nested_sandbox
def test_nested(): pass
@pytest.mark.requires_network
def test_network(): pass
@pytest.mark.requires_provider_home("codex")
def test_home(): pass
@pytest.mark.requires_browser
def test_browser(): pass
def test_plain(): pass
""")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    if available:
        result.assert_outcomes(passed=5)
        assert not any("tests skipped:" in line for line in result.outlines)
    else:
        result.assert_outcomes(passed=1, skipped=4)
        for capability in ("nested_sandbox", "network", "provider_home", "browser"):
            result.stdout.fnmatch_lines([
                f"1 tests skipped: {capability} unavailable (denied by test sandbox)"
            ])


@pytest.mark.parametrize("ci,installed", [(True, True), (True, False), (False, True)])
def test_browser_probe_failure_fails_under_ci_when_chrome_is_installed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ci: bool, installed: bool,
) -> None:
    if ci:
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.delenv("CI", raising=False)
    source = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(source + f"""
def _browser_executable():
    return {"'/fake-chrome'" if installed else "None"}
def probe_browser():
    return "probe timed out"
""")
    pytester.makeini("[pytest]\nmarkers =\n    requires_browser: headless browser\n")
    pytester.makepyfile("""
import pytest
@pytest.mark.requires_browser
def test_browser(): pass
@pytest.mark.requires_browser
@pytest.mark.skipif(True, reason="platform gated")
def test_gated_browser(): pass
""")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs")
    if ci and installed:
        # The platform-gated test still skips; only the runnable one fails.
        result.assert_outcomes(errors=1, skipped=1)
        result.stdout.fnmatch_lines(["*browser installed but unusable under CI (probe timed out)*"])
        assert not any("tests skipped: browser" in line for line in result.outlines)
    else:
        result.assert_outcomes(skipped=2)
        result.stdout.fnmatch_lines(["1 tests skipped: browser unavailable (probe timed out)"])


@pytest.mark.parametrize("provider,key", [
    ("codex", "OPENAI_API_KEY"),
    ("codex", "CODEX_API_KEY"),
    ("claude", "ANTHROPIC_API_KEY"),
    ("claude", "ANTHROPIC_AUTH_TOKEN"),
    ("claude", "CLAUDE_CODE_OAUTH_TOKEN"),
])
def test_environment_credentials_run_without_provider_home(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, provider: str, key: str,
) -> None:
    import conftest

    for keys in conftest._PROVIDER_AUTH_ENV_KEYS.values():
        for name in keys:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, "synthetic-test-credential")
    missing = pytester.path / "missing-provider-home"
    source = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(source + f"""
_REAL_PROVIDER_HOMES = {{"codex": Path({str(missing)!r}), "claude": Path({str(missing)!r})}}
""")
    pytester.makeini("[pytest]\nmarkers =\n    requires_provider_home: provider authentication\n    requires_nested_sandbox: nested sandbox\n")
    variable = "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"
    pytester.makepyfile(f"""
import os
from pathlib import Path
import pytest
from conftest import probe_provider_home

@pytest.mark.requires_provider_home({provider!r})
def test_environment_auth():
    assert os.environ[{key!r}] == "synthetic-test-credential"
    assert probe_provider_home({provider!r}) is None
    home = Path(os.environ[{variable!r}])
    assert home.is_dir()
    assert home.is_relative_to(Path(os.environ["XDG_STATE_HOME"]).parent)
    assert home != Path({str(missing)!r})
    assert not (home / "auth.json").exists()
""")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not any("provider_home unavailable" in line for line in result.outlines)
    assert not missing.exists()


def test_opt_in_claude_canaries_keep_directory_authentication(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    for keys in conftest._PROVIDER_AUTH_ENV_KEYS.values():
        for name in keys:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPEC_LINUX_CLAUDE_REAL_PROVIDER", "1")
    monkeypatch.setenv("SPEC_LINUX_CLAUDE_REVIEW_CANARY", "1")
    home = pytester.path / "operator-claude"
    home.mkdir()
    (home / ".credentials.json").write_text("synthetic-directory-login", encoding="utf-8")
    source = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(source + f"""
_REAL_PROVIDER_HOMES["claude"] = Path({str(home)!r})

@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    import json
    import sys
    monkeypatch.setattr(sys, "platform", "linux")
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw:
        "/fake/claude" if name == "claude" else real_which(name, *a, **kw))
    real_run = subprocess.run
    def run(argv, **kwargs):
        if Path(argv[0]).name != "claude":
            return real_run(argv, **kwargs)
        env = kwargs["env"]
        config = Path(env["CLAUDE_CONFIG_DIR"])
        assert config == Path({str(home)!r})
        assert (config / ".credentials.json").read_text(encoding="utf-8") == "synthetic-directory-login"
        assert Path(env["HOME"]) != config.parent
        if "cwd" in kwargs and "--tools" in argv and argv[argv.index("--tools") + 1] == "Bash":
            (Path(kwargs["cwd"]) / "hardlink-result").write_text("BLOCKED", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0,
            json.dumps({{"type": "result", "result": "CANARIES_INACCESSIBLE"}}), "")
    monkeypatch.setattr(subprocess, "run", run)
""")
    pytester.makeini("[pytest]\nmarkers =\n    requires_provider_home: provider authentication\n    requires_nested_sandbox: nested sandbox\n")
    tests = Path(__file__).parent
    pytester.makepyfile(
        test_hardlink=(tests / "test_agent_git_isolation.py").read_text(encoding="utf-8"),
        test_reviewer=(tests / "test_linux_claude_review_isolation.py").read_text(encoding="utf-8"),
    )
    result = pytester.runpytest_subprocess(
        "test_hardlink.py::test_real_claude_sandbox_blocks_hardlinks_from_read_only_git_metadata",
        "test_reviewer.py::test_real_claude_reviewer_cannot_read_outside_or_environment_canaries",
        "-p", "no:cacheprovider",
    )
    result.assert_outcomes(passed=2)


def test_all_linux_credentialed_canaries_declare_provider_home() -> None:
    """Derive the live-canary inventory from their opt-in environment guards."""
    import ast
    import re

    found = []
    for path in Path(__file__).parent.glob("test_*.py"):
        module = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            node.targets[0].id: node.value.value
            for node in module.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        }
        for function in (node for node in ast.walk(module) if isinstance(node, ast.FunctionDef)):
            if not function.name.startswith("test_"):
                continue
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                if ast.unparse(call.func) != "os.environ.get" or not call.args:
                    continue
                arg = call.args[0]
                name = arg.value if isinstance(arg, ast.Constant) else constants.get(getattr(arg, "id", ""))
                if not isinstance(name, str) or not re.fullmatch(
                    r"SPEC_LINUX_(CLAUDE|CODEX)_(REAL_PROVIDER|REVIEW_CANARY)", name,
                ):
                    continue
                provider = "claude" if "CLAUDE" in name else "codex"
                assert any(
                    isinstance(decorator, ast.Call)
                    and ast.unparse(decorator.func) == "pytest.mark.requires_provider_home"
                    and decorator.args and isinstance(decorator.args[0], ast.Constant)
                    and decorator.args[0].value == provider
                    for decorator in function.decorator_list
                ), f"{path.name}::{function.name} must preserve {provider} authentication"
                found.append(f"{path.name}::{function.name}")
    assert found
    print("Credentialed Linux canaries: " + ", ".join(sorted(found)))


def test_environment_auth_keys_cover_doctor_credential_hints() -> None:
    import conftest
    from spec_runtime.doctor import _PROVIDER_AUTH_ENV_HINTS

    expected = {
        provider: {key for key in keys if key.endswith(("_KEY", "_TOKEN"))}
        for provider, keys in _PROVIDER_AUTH_ENV_HINTS.items()
    }
    assert {provider: set(keys) for provider, keys in conftest._PROVIDER_AUTH_ENV_KEYS.items()} == expected


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
@posix_only
def test_browser_probe_stops_children_before_removing_profile(monkeypatch, outcome):
    import signal
    import subprocess
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    import conftest

    events = []

    @contextmanager
    def home():
        yield "/probe-home"
        events.append("cleanup")

    process = MagicMock(pid=12345, returncode=0 if outcome == "success" else 1)
    process.__enter__.return_value = process

    def communicate(timeout=None):
        events.append("communicate")
        if outcome == "timeout" and timeout is not None:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return ("<html></html>" if outcome == "success" else "", "browser failed")

    process.communicate.side_effect = communicate
    launch = MagicMock(return_value=process)
    monkeypatch.setattr(conftest.shutil, "which", lambda _: "/fake-chrome")
    monkeypatch.setattr(conftest, "_browser_probe_home", home)
    monkeypatch.setattr(conftest.subprocess, "Popen", launch)
    monkeypatch.setattr(conftest.os, "killpg", lambda pid, sig: events.append((pid, sig)))

    reason = conftest.probe_browser()

    assert (reason is None) == (outcome == "success")
    assert launch.call_args.kwargs["start_new_session"] is True
    assert events == ["communicate", (12345, signal.SIGKILL), "communicate", "cleanup"]
    assert launch.call_args.kwargs["env"]["HOME"] == "/probe-home"


@pytest.mark.parametrize("retried", [True, False])
def test_browser_probe_profile_cleanup_retries_only_directory_not_empty(monkeypatch, retried):
    import errno
    from unittest.mock import MagicMock

    import conftest

    error_number = errno.ENOTEMPTY if retried else errno.EACCES
    home = MagicMock(name="temporary-home")
    home.cleanup.side_effect = [OSError(error_number, "cleanup failed"), None]
    monkeypatch.setattr(conftest.tempfile, "TemporaryDirectory", lambda: home)
    sleep = MagicMock()
    monkeypatch.setattr(conftest.time, "sleep", sleep)

    if retried:
        with conftest._browser_probe_home():
            pass
        assert home.cleanup.call_count == 2
        sleep.assert_called_once_with(0.05)
    else:
        with pytest.raises(OSError) as exc, conftest._browser_probe_home():
            pass
        assert exc.value.errno == errno.EACCES
        assert home.cleanup.call_count == 1
        sleep.assert_not_called()
