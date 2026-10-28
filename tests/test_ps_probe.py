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
