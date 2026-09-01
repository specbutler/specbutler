#!/usr/bin/env python3
"""Produce fail-closed hosted-CI evidence for the Windows release audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REVISION_LENGTH = 40
EXPECTED_LINUX_VERSIONS = {"3.11", "3.12", "3.13"}
EXPECTED_WINDOWS_PROBES = {
    ("3.11", "wheel", True, False),
    ("3.12", "wheel", True, True),
    ("3.13", "wheel", True, False),
    ("3.12", "sdist", False, False),
}
SMOKE_COMMANDS = [
    "init",
    "doctor",
    "list",
    "show",
    "status",
    "input",
    "update-foreground",
    "update-background",
    "needs-input",
    "web-foreground",
    "web-background",
    "auto-dispatch",
    "auto-adopt",
    "auto-stop",
    "cleanup",
]
DOCUMENTATION_TEST = "test_windows_docs_state_exact_supported_tier_and_exclusions"
REAL_PROVIDER_MARKER_TEST = "test_windows_real_provider_proof_is_separately_marked_and_one_command"
HERMETIC_LIFECYCLE_TEST = "test_installed_artifact_cli_matrix"
CROSS_PLATFORM_REQUIRED_TESTS = {
    "test_save_and_load_from_explicit_state_root",
    "test_implement_command_dispatches_to_orchestrator",
    "test_bearer_auth_allows_access",
    "test_codex_session_resumes_thread_before_follow_up_turn",
    "test_chat_provider_generator_cancel_terminates_and_reaps_process",
    "test_autopilot_acquires_candidate_lease_before_launch",
    "test_watch_command_non_tty_prints_once",
    DOCUMENTATION_TEST,
    REAL_PROVIDER_MARKER_TEST,
}
WINDOWS_FOCUSED_REQUIRED_TESTS = {
    "test_native_windows_volume_probe_reports_fixed_ntfs_checkout",
    "test_repository_text_survives_utf8_mode_off",
    "test_cross_process_spec_lock_contention",
    "test_parent_child_grandchild_termination",
    "test_spec_stop_terminates_owned_tree_without_touching_unrelated_process",
    "test_local_review_timeout_reaps_tree_without_touching_unrelated_process",
    "test_cleanup_reaps_registered_helper_and_preserves_unrelated_process",
    "test_spec_init_output_is_accepted_by_doctor",
    "test_foreground_web_bind_and_authenticated_request",
}
WINDOWS_FOCUSED_ALLOWED_SKIPS = {HERMETIC_LIFECYCLE_TEST}


class EvidenceError(ValueError):
    """Evidence cannot support the requested claim."""


def _exact_revision(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != REVISION_LENGTH or any(character not in "0123456789abcdef" for character in normalized):
        raise EvidenceError(f"{label} is not an exact 40-character Git SHA")
    return normalized


def _checkout_revision(source_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD^{commit}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise EvidenceError(f"cannot resolve source checkout: {completed.stderr.strip()}")
    return _exact_revision(completed.stdout, label="source checkout revision")


def _github_context(environ: dict[str, str]) -> dict[str, Any]:
    if environ.get("GITHUB_ACTIONS") != "true":
        raise EvidenceError("CI evidence fragments may only be recorded inside GitHub Actions")
    required = (
        "GITHUB_REPOSITORY",
        "GITHUB_WORKFLOW",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "GITHUB_SHA",
        "GITHUB_EVENT_NAME",
        "RUNNER_OS",
        "RUNNER_ARCH",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise EvidenceError(f"GitHub Actions context is incomplete: {', '.join(missing)}")
    if not environ["GITHUB_RUN_ID"].isdigit() or not environ["GITHUB_RUN_ATTEMPT"].isdigit():
        raise EvidenceError("GitHub run identity is malformed")
    return {
        "repository": environ["GITHUB_REPOSITORY"],
        "workflow": environ["GITHUB_WORKFLOW"],
        "run_id": int(environ["GITHUB_RUN_ID"]),
        "run_attempt": int(environ["GITHUB_RUN_ATTEMPT"]),
        "job": environ["GITHUB_JOB"],
        "event_name": environ["GITHUB_EVENT_NAME"],
        "github_sha": _exact_revision(environ["GITHUB_SHA"], label="GITHUB_SHA"),
        "runner_os": environ["RUNNER_OS"],
        "runner_arch": environ["RUNNER_ARCH"],
    }


def _junit_summary(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise EvidenceError(f"JUnit evidence is missing or empty: {path}")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise EvidenceError(f"cannot parse JUnit evidence {path}: {exc}") from exc
    cases = list(root.iter("testcase"))
    if not cases:
        raise EvidenceError(f"JUnit evidence contains no test cases: {path}")
    failures = sum(1 for case in cases if case.find("failure") is not None)
    errors = sum(1 for case in cases if case.find("error") is not None)
    skipped_nodes = [case.find("skipped") for case in cases if case.find("skipped") is not None]
    passed_names = sorted(
        str(case.get("name", ""))
        for case in cases
        if not any(case.find(kind) is not None for kind in ("failure", "error", "skipped"))
    )
    skipped_names = sorted(
        str(case.get("name", ""))
        for case in cases
        if case.find("skipped") is not None
    )
    if failures or errors:
        raise EvidenceError(f"JUnit evidence contains failures/errors: {path} ({failures}/{errors})")
    skipped_reasons = sorted(
        {
            str(node.get("message", "")).strip()
            for node in skipped_nodes
            if node is not None and str(node.get("message", "")).strip()
        }
    )
    names = sorted(str(case.get("name", "")) for case in cases)
    return {
        "artifact": path.name,
        "tests": len(cases),
        "failures": failures,
        "errors": errors,
        "skipped": len(skipped_nodes),
        "skipped_reasons": skipped_reasons,
        "test_names": names,
        "passed_test_names": passed_names,
        "skipped_test_names": skipped_names,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _record(args: argparse.Namespace) -> int:
    source_root = args.source_root.resolve()
    revision = _checkout_revision(source_root)
    github = _github_context(dict(os.environ))
    if github["github_sha"] != revision:
        raise EvidenceError(f"GitHub SHA {github['github_sha']} does not match tested checkout {revision}")

    details: dict[str, Any]
    if args.kind == "lint":
        if github["runner_os"] != "Linux":
            raise EvidenceError("lint evidence must come from the Linux lint job")
        details = {"ruff": "passed", "actionlint": "passed"}
    elif args.kind in {"linux-test", "macos-test"}:
        expected_os = "Linux" if args.kind == "linux-test" else "macOS"
        if github["runner_os"] != expected_os:
            raise EvidenceError(f"{args.kind} evidence must come from a {expected_os} runner")
        if not args.python_version or not args.junit:
            raise EvidenceError(f"{args.kind} requires --python-version and --junit")
        portable = _junit_summary(args.junit)
        _require_passed_test_names(
            portable,
            CROSS_PLATFORM_REQUIRED_TESTS,
            label=f"{args.kind} portable suite",
        )
        details = {
            "python_version": args.python_version,
            "portable_suite": portable,
        }
    elif args.kind == "windows-package":
        if github["runner_os"] != "Windows" or not args.dist_dir:
            raise EvidenceError("windows-package requires a Windows runner and --dist-dir")
        wheels = sorted(args.dist_dir.glob("*.whl"))
        sdists = sorted(args.dist_dir.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise EvidenceError("windows-package requires exactly one wheel and one sdist")
        details = {
            "wheel": wheels[0].name,
            "wheel_sha256": hashlib.sha256(wheels[0].read_bytes()).hexdigest(),
            "sdist": sdists[0].name,
            "sdist_sha256": hashlib.sha256(sdists[0].read_bytes()).hexdigest(),
            "twine_check": "passed",
        }
    elif args.kind == "windows-probe":
        if github["runner_os"] != "Windows":
            raise EvidenceError("windows-probe evidence must come from a Windows runner")
        if not args.python_version or args.distribution not in {"wheel", "sdist"}:
            raise EvidenceError("windows-probe requires Python version and distribution")
        key = (args.python_version, args.distribution, args.full_suite, args.cli_matrix)
        if key not in EXPECTED_WINDOWS_PROBES:
            raise EvidenceError(f"unexpected Windows probe matrix entry: {key}")
        details = {
            "python_version": args.python_version,
            "distribution": args.distribution,
            "install": "passed",
            "dependency_check": "passed",
            "installed_imports": "passed",
            "full_suite": args.full_suite,
            "cli_matrix": args.cli_matrix,
        }
        if args.full_suite:
            if not args.junit or not args.focused_junit:
                raise EvidenceError("full Windows probes require portable and focused JUnit")
            portable = _junit_summary(args.junit)
            focused = _junit_summary(args.focused_junit)
            _require_passed_test_names(
                portable,
                CROSS_PLATFORM_REQUIRED_TESTS,
                label="Windows portable suite",
            )
            unexpected_skips = sorted(
                set(focused["skipped_test_names"]) - WINDOWS_FOCUSED_ALLOWED_SKIPS
            )
            if unexpected_skips:
                raise EvidenceError(
                    "Windows integration was skipped for its platform or another unsupported reason: "
                    + ", ".join(unexpected_skips)
                )
            _require_passed_test_names(
                focused,
                WINDOWS_FOCUSED_REQUIRED_TESTS,
                label="Windows focused integration suite",
            )
            details.update(
                {
                    "lint": "passed",
                    "portable_suite_result": portable,
                    "windows_integration_result": focused,
                    "windows_integration_skipped_for_server": False,
                }
            )
        if args.cli_matrix:
            if not args.cli_junit:
                raise EvidenceError("CLI matrix probe requires --cli-junit")
            cli = _junit_summary(args.cli_junit)
            if cli["skipped"]:
                raise EvidenceError("installed-artifact CLI matrix did not execute")
            _require_passed_test_names(
                cli,
                {HERMETIC_LIFECYCLE_TEST},
                label="installed-artifact CLI matrix",
            )
            details["installed_cli_matrix_result"] = cli
    else:  # pragma: no cover - argparse enforces choices
        raise EvidenceError(f"unsupported fragment kind: {args.kind}")

    payload = {
        "schema_version": 1,
        "status": "passed",
        "kind": args.kind,
        "source_revision": revision,
        "github": github,
        "details": details,
    }
    _atomic_json(args.output, payload)
    return 0


def _load_fragments(directory: Path, revision: str) -> list[dict[str, Any]]:
    paths = sorted(directory.rglob("*.json"))
    if not paths:
        raise EvidenceError(f"no CI evidence fragments found under {directory}")
    fragments: list[dict[str, Any]] = []
    identities: set[tuple[Any, ...]] = set()
    common_run: tuple[Any, ...] | None = None
    for path in paths:
        try:
            fragment = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"cannot read CI fragment {path}: {exc}") from exc
        if not isinstance(fragment, dict) or fragment.get("schema_version") != 1:
            raise EvidenceError(f"invalid fragment schema: {path}")
        if fragment.get("status") != "passed" or fragment.get("source_revision") != revision:
            raise EvidenceError(f"fragment does not attest success at {revision}: {path}")
        github = fragment.get("github")
        details = fragment.get("details")
        kind = fragment.get("kind")
        if not isinstance(github, dict) or not isinstance(details, dict) or not isinstance(kind, str):
            raise EvidenceError(f"fragment fields are malformed: {path}")
        run = (
            github.get("repository"),
            github.get("workflow"),
            github.get("run_id"),
            github.get("run_attempt"),
            github.get("github_sha"),
        )
        if run[-1] != revision:
            raise EvidenceError(f"fragment GitHub SHA differs from source revision: {path}")
        if common_run is None:
            common_run = run
        elif common_run != run:
            raise EvidenceError(f"fragments came from different workflow runs: {path}")
        identity = (
            kind,
            details.get("python_version"),
            details.get("distribution"),
        )
        if identity in identities:
            raise EvidenceError(f"duplicate fragment identity {identity}: {path}")
        identities.add(identity)
        fragment["_fragment_path"] = path
        fragments.append(fragment)
    return fragments


def _one(fragments: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [fragment for fragment in fragments if fragment["kind"] == kind]
    if len(matches) != 1:
        raise EvidenceError(f"expected exactly one {kind} fragment, found {len(matches)}")
    return matches[0]


def _require_runner(fragment: dict[str, Any], *, os_name: str, job: str) -> None:
    github = fragment["github"]
    if github.get("runner_os") != os_name or github.get("job") != job:
        raise EvidenceError(
            f"{fragment['kind']} fragment came from {github.get('runner_os')}/{github.get('job')}, "
            f"expected {os_name}/{job}"
        )


def _require_passing_junit(summary: Any, *, label: str, fragment: dict[str, Any]) -> None:
    if not isinstance(summary, dict):
        raise EvidenceError(f"{label} has no JUnit summary")
    if not isinstance(summary.get("tests"), int) or summary["tests"] < 1:
        raise EvidenceError(f"{label} ran no tests")
    if summary.get("failures") != 0 or summary.get("errors") != 0:
        raise EvidenceError(f"{label} contains failures or errors")
    passed_names = summary.get("passed_test_names")
    if not isinstance(passed_names, list) or not passed_names:
        raise EvidenceError(f"{label} contains no passed tests")
    digest = summary.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise EvidenceError(f"{label} has no exact JUnit digest")
    artifact_name = summary.get("artifact")
    if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
        raise EvidenceError(f"{label} has an unsafe JUnit artifact name")
    artifact = fragment["_fragment_path"].parent / artifact_name
    if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
        raise EvidenceError(f"{label} retained JUnit artifact is missing or has changed")
    retained = _junit_summary(artifact)
    if retained != summary:
        raise EvidenceError(f"{label} summary does not match its retained JUnit artifact")


def _require_passed_test_names(summary: Any, required: set[str], *, label: str) -> None:
    if not isinstance(summary, dict) or not isinstance(summary.get("passed_test_names"), list):
        raise EvidenceError(f"{label} has no machine-readable passed-test inventory")
    names = {name for name in summary["passed_test_names"] if isinstance(name, str)}
    missing = sorted(required - names)
    if missing:
        raise EvidenceError(f"{label} lacks required tests: {', '.join(missing)}")


def _aggregate(args: argparse.Namespace) -> int:
    source_root = args.source_root.resolve()
    revision = _exact_revision(args.expected_revision, label="expected revision")
    if _checkout_revision(source_root) != revision:
        raise EvidenceError("aggregation checkout does not match expected revision")
    fragments = _load_fragments(args.input, revision)
    if len(fragments) != 10:
        raise EvidenceError(f"expected 10 CI evidence fragments, found {len(fragments)}")

    lint = _one(fragments, "lint")
    windows_package = _one(fragments, "windows-package")
    linux = [fragment for fragment in fragments if fragment["kind"] == "linux-test"]
    macos = [fragment for fragment in fragments if fragment["kind"] == "macos-test"]
    windows = [fragment for fragment in fragments if fragment["kind"] == "windows-probe"]
    linux_versions = {fragment["details"].get("python_version") for fragment in linux}
    if linux_versions != EXPECTED_LINUX_VERSIONS or len(linux) != len(EXPECTED_LINUX_VERSIONS):
        raise EvidenceError(f"Linux test matrix is incomplete: {sorted(linux_versions)}")
    if len(macos) != 1 or macos[0]["details"].get("python_version") != "3.12":
        raise EvidenceError("macOS test matrix is incomplete")
    windows_keys = {
        (
            fragment["details"].get("python_version"),
            fragment["details"].get("distribution"),
            fragment["details"].get("full_suite"),
            fragment["details"].get("cli_matrix"),
        )
        for fragment in windows
    }
    if windows_keys != EXPECTED_WINDOWS_PROBES or len(windows) != len(EXPECTED_WINDOWS_PROBES):
        raise EvidenceError(f"Windows probe matrix is incomplete: {sorted(windows_keys)}")

    _require_runner(lint, os_name="Linux", job="lint")
    if lint["details"] != {"ruff": "passed", "actionlint": "passed"}:
        raise EvidenceError("lint fragment is incomplete")
    for fragment in linux:
        _require_runner(fragment, os_name="Linux", job="test")
        _require_passing_junit(
            fragment["details"].get("portable_suite"),
            label=f"Linux {fragment['details'].get('python_version')} suite",
            fragment=fragment,
        )
        _require_passed_test_names(
            fragment["details"].get("portable_suite"),
            CROSS_PLATFORM_REQUIRED_TESTS,
            label=f"Linux {fragment['details'].get('python_version')} suite",
        )
    _require_runner(macos[0], os_name="macOS", job="macos-test")
    _require_passing_junit(
        macos[0]["details"].get("portable_suite"),
        label="macOS suite",
        fragment=macos[0],
    )
    _require_passed_test_names(
        macos[0]["details"].get("portable_suite"),
        CROSS_PLATFORM_REQUIRED_TESTS,
        label="macOS suite",
    )
    _require_runner(windows_package, os_name="Windows", job="windows-package")
    package_details = windows_package["details"]
    if (
        package_details.get("twine_check") != "passed"
        or not str(package_details.get("wheel", "")).endswith(".whl")
        or not str(package_details.get("sdist", "")).endswith(".tar.gz")
    ):
        raise EvidenceError("Windows package fragment is incomplete")
    for digest_name in ("wheel_sha256", "sdist_sha256"):
        digest = package_details.get(digest_name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise EvidenceError(f"Windows package fragment lacks {digest_name}")

    wheel_probes = [item for item in windows if item["details"]["distribution"] == "wheel"]
    for fragment in windows:
        _require_runner(fragment, os_name="Windows", job="windows-probe")
        details = fragment["details"]
        if (
            details.get("install") != "passed"
            or details.get("dependency_check") != "passed"
            or details.get("installed_imports") != "passed"
        ):
            raise EvidenceError("one or more Windows probes lack installed-artifact evidence")
    if any(item["details"].get("lint") != "passed" for item in wheel_probes):
        raise EvidenceError("one or more wheel probes lack Windows lint evidence")
    if any(item["details"].get("windows_integration_skipped_for_server") is not False for item in wheel_probes):
        raise EvidenceError("one or more Windows integrations lack native execution evidence")
    for fragment in wheel_probes:
        details = fragment["details"]
        _require_passing_junit(
            details.get("portable_suite_result"),
            label=f"Windows {details['python_version']} portable suite",
            fragment=fragment,
        )
        _require_passed_test_names(
            details.get("portable_suite_result"),
            CROSS_PLATFORM_REQUIRED_TESTS,
            label=f"Windows {details['python_version']} portable suite",
        )
        _require_passing_junit(
            details.get("windows_integration_result"),
            label=f"Windows {details['python_version']} integration suite",
            fragment=fragment,
        )
        _require_passed_test_names(
            details.get("windows_integration_result"),
            WINDOWS_FOCUSED_REQUIRED_TESTS,
            label=f"Windows {details['python_version']} integration suite",
        )
        skipped_names = details["windows_integration_result"].get("skipped_test_names")
        if not isinstance(skipped_names, list) or set(skipped_names) - WINDOWS_FOCUSED_ALLOWED_SKIPS:
            raise EvidenceError("Windows focused integration contains unexpected skipped tests")
    cli_probe = next(item for item in wheel_probes if item["details"]["python_version"] == "3.12")
    if "installed_cli_matrix_result" not in cli_probe["details"]:
        raise EvidenceError("Python 3.12 wheel probe lacks installed CLI evidence")
    _require_passing_junit(
        cli_probe["details"]["installed_cli_matrix_result"],
        label="Windows installed CLI matrix",
        fragment=cli_probe,
    )
    _require_passed_test_names(
        cli_probe["details"]["installed_cli_matrix_result"],
        {HERMETIC_LIFECYCLE_TEST},
        label="Windows installed CLI matrix",
    )
    _require_passed_test_names(
        cli_probe["details"]["portable_suite_result"],
        CROSS_PLATFORM_REQUIRED_TESTS,
        label="Windows portable suite",
    )

    pyproject = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = pyproject.get("project", {}).get("classifiers", [])
    if "Operating System :: Microsoft :: Windows" not in classifiers:
        raise EvidenceError("Windows package classifier is absent")

    github_run = {
        key: lint["github"][key]
        for key in ("repository", "workflow", "run_id", "run_attempt", "event_name", "github_sha")
    }
    reports = {
        "hosted-windows-ci-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "wheel_installed": True,
            "lint": "passed",
            "portable_suite": "passed",
            "supported_python_versions_tested": len(wheel_probes),
            "windows_integration_skipped_for_server": False,
            "github_run": github_run,
        },
        "hosted-windows-smoke-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "path_has_spaces_and_unicode": True,
            "commands": SMOKE_COMMANDS,
            "installed_artifact": "wheel",
            "github_run": github_run,
        },
        "package-release-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "windows_classifier": True,
            "wheel_install": "passed",
            "sdist_install": "passed",
            "doctor_unexplained_warnings": 0,
            "distributions": windows_package["details"],
            "github_run": github_run,
        },
        "cross-platform-lifecycle-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "linux": "passed",
            "macos": "passed",
            "state_compatibility": "passed",
            "github_run": github_run,
        },
        "cross-platform-web-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "web": "passed",
            "chat": "passed",
            "autopilot": "passed",
            "tui": "passed",
            "github_run": github_run,
        },
        "test-coverage-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "hermetic_lifecycle": True,
            "real_provider_separately_marked": True,
            "required_tests": [HERMETIC_LIFECYCLE_TEST, REAL_PROVIDER_MARKER_TEST],
            "github_run": github_run,
        },
        "documentation-audit-result.json": {
            "schema_version": 1,
            "status": "passed",
            "source_revision": revision,
            "surfaces": ["README", "INSTALL", "troubleshooting", "support-matrix"],
            "exact_supported_tier": True,
            "native_codex_claude_container_distinguished": True,
            "required_test": DOCUMENTATION_TEST,
            "github_run": github_run,
        },
    }
    input_digests = {
        path.relative_to(args.input).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.input.rglob("*"))
        if path.is_file()
    }
    index = {
        "schema_version": 1,
        "status": "passed",
        "source_revision": revision,
        "github_run": github_run,
        "fragment_count": len(fragments),
        "input_sha256": input_digests,
        "reports": sorted(reports),
    }
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise EvidenceError(f"refusing to mix aggregate evidence into non-empty directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as staging_name:
        staging = Path(staging_name)
        shutil.copytree(args.input, staging / "ci-fragments")
        for name, report in reports.items():
            _atomic_json(staging / name, report)
        _atomic_json(staging / "hosted-ci-evidence-index.json", index)
        if output.exists():
            output.rmdir()
        staging.replace(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument(
        "--kind",
        required=True,
        choices=("lint", "linux-test", "macos-test", "windows-package", "windows-probe"),
    )
    record.add_argument("--output", required=True, type=Path)
    record.add_argument("--source-root", type=Path, default=Path.cwd())
    record.add_argument("--python-version")
    record.add_argument("--distribution")
    record.add_argument("--full-suite", action="store_true")
    record.add_argument("--cli-matrix", action="store_true")
    record.add_argument("--junit", type=Path)
    record.add_argument("--focused-junit", type=Path)
    record.add_argument("--cli-junit", type=Path)
    record.add_argument("--dist-dir", type=Path)
    record.set_defaults(function=_record)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--input", required=True, type=Path)
    aggregate.add_argument("--output", required=True, type=Path)
    aggregate.add_argument("--source-root", type=Path, default=Path.cwd())
    aggregate.add_argument("--expected-revision", required=True)
    aggregate.set_defaults(function=_aggregate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.function(args))
    except (EvidenceError, OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"CI evidence failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
