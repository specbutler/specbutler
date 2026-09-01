#!/usr/bin/env python3
"""Produce local Windows acceptance artifacts from executable evidence.

This helper is intentionally stricter than the manifest auditor.  The auditor
checks the shape of retained evidence; this producer first checks that the
named native tests actually passed, validates the real-proof artifacts field by
field, and runs direct package, path, watch, documentation, and secret probes.
No result file is written until every prerequisite for that result has passed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

LOCAL_ARTIFACTS = (
    "native-command-matrix-result.json",
    "lifecycle-fault-matrix-result.json",
    "isolation-result.json",
    "windows-path-result.json",
    "review-isolation-result.json",
    "native-claude-result.json",
    "update-result.json",
    "test-coverage-result.json",
    "web-action-result.json",
    "watch-result.json",
    "web-integration-result.json",
    "documentation-audit-result.json",
    "package-release-result.json",
)

EXTERNAL_ARTIFACTS = (
    "cross-platform-lifecycle-result.json",
    "linux-claude-web-result.json",
    "cross-platform-web-result.json",
    "hosted-windows-ci-result.json",
    "hosted-windows-smoke-result.json",
)

REQUIRED_TESTS: Mapping[str, tuple[str, ...]] = {
    "native-command-matrix-result.json": (
        "test_installed_artifact_cli_matrix",
        "test_cmd_task_launches_spec_authoring_session",
        "test_cmd_task_accepts_codex_agent",
        "test_phase_scoping_launches_codex_and_renames_branch",
        "test_spec_stop_terminates_owned_tree_without_touching_unrelated_process",
        "test_gc_command_dry_run_does_not_mutate",
    ),
    "lifecycle-fault-matrix-result.json": (
        "test_spec_stop_terminates_owned_tree_without_touching_unrelated_process",
        "test_local_review_timeout_reaps_tree_without_touching_unrelated_process",
        "test_required_checks_failure_triggers_implement_retry",
        "test_local_mode_request_changes_feeds_retry_state",
        "test_cmd_run_auto_resumes_retryable_failed_implement_run",
        "test_gc_command_marks_stale_run_as_failed",
        "test_merge_conflict_triggers_implement_retry",
        "test_installed_artifact_cli_matrix",
    ),
    "isolation-result.json": (
        "test_windows_default_copies_auth_without_symlink_privilege",
        "test_write_codex_isolated_home_replaces_planted_windows_junction",
        "test_windows_codex_authoring_passes_host_gh_token_without_profile_access",
    ),
    "windows-path-result.json": (
        "test_repository_text_survives_utf8_mode_off",
        "test_github_cli_utf8_json_and_crlf_survive_cp1252_locale",
        "test_installed_artifact_cli_matrix",
    ),
    "review-isolation-result.json": (
        "test_native_sandbox_denies_operator_secret_and_sibling_write",
        "test_temporary_debugger_uses_private_clone_for_validated_surviving_workspace",
        "test_local_review_timeout_reaps_tree_without_touching_unrelated_process",
    ),
    "native-claude-result.json": (
        "test_native_windows_hides_claude_chat_when_host_sandbox_is_unavailable",
        "test_native_windows_direct_claude_stream_fails_before_spawn",
        "test_local_review_fails_before_cli_discovery_or_subprocess",
        "test_block_debugger_fails_before_context_or_agent_process",
    ),
    "update-result.json": (
        "test_installed_artifact_cli_matrix",
        "test_background_supervised_refresh_retires_domain_token_and_lock",
        "test_windows_detached_workflows_survive_launcher_and_stop_by_identity[update-refresh]",
    ),
    "test-coverage-result.json": (
        "test_windows_real_provider_proof_is_separately_marked_and_one_command",
        "test_installed_artifact_cli_matrix",
    ),
    "web-action-result.json": (
        "test_implement_spec",
        "test_stop_spec_uses_managed_process_when_run_metadata_exists",
        "test_process_registry_is_scoped_to_app_and_cleared_on_shutdown",
        "test_create_session_cleans_up_on_non_runtime_error",
    ),
    "watch-result.json": (
        "test_watch_command_non_tty_prints_once",
        "test_windows_available_memory_caps_computed_concurrency",
        "test_read_process_identity_delegates_to_portable_boundary",
        "test_chat_provider_process_streams_ordinary_success",
        "test_chat_provider_generator_cancel_terminates_and_reaps_process",
    ),
    "web-integration-result.json": (
        "test_bearer_auth_allows_access",
        "test_initial_prompt_reconnect_after_turn_completes",
        "test_codex_session_resumes_thread_before_follow_up_turn",
        "test_app_shutdown_stops_bridges_cancels_turns_and_clears_registry",
        "test_windows_background_web_server_survives_launcher_and_stops_by_token",
        "test_is_server_running_rejects_stale_pid",
    ),
    "documentation-audit-result.json": (
        "test_windows_docs_state_exact_supported_tier_and_exclusions",
    ),
    "package-release-result.json": (
        "test_installed_artifact_cli_matrix",
    ),
}


class EvidenceError(RuntimeError):
    """A prerequisite was missing, skipped, failed, or contradicted a claim."""


def _test_name_matches(actual: str, required: str) -> bool:
    return actual == required or (
        "[" not in required and actual.startswith(required + "[")
    )


def passed_tests(*reports: Path) -> set[str]:
    """Return passed JUnit testcase names, rejecting malformed reports."""
    passed: set[str] = set()
    for report in reports:
        try:
            root = ET.parse(report).getroot()
        except (OSError, ET.ParseError) as exc:
            raise EvidenceError(f"invalid JUnit report {report}: {exc}") from exc
        suites = [root] if root.tag == "testsuite" else []
        suites.extend(root.findall(".//testsuite"))
        for suite in suites:
            try:
                failures = int(suite.get("failures", "0"))
                errors = int(suite.get("errors", "0"))
            except ValueError as exc:
                raise EvidenceError(f"invalid JUnit counters in {report}") from exc
            if failures or errors:
                raise EvidenceError(
                    f"JUnit report is not green: {report} "
                    f"({failures} failures, {errors} errors)"
                )
        cases = root.findall(".//testcase")
        if not cases:
            raise EvidenceError(f"JUnit report has no test cases: {report}")
        for case in cases:
            name = str(case.get("name") or "").strip()
            if not name:
                raise EvidenceError(f"JUnit testcase has no name: {report}")
            if not any(case.find(kind) is not None for kind in ("failure", "error", "skipped")):
                passed.add(name)
    return passed


def require_tests(passed: set[str], required: Iterable[str]) -> list[str]:
    proven: list[str] = []
    for name in required:
        matches = sorted(actual for actual in passed if _test_name_matches(actual, name))
        if not matches:
            raise EvidenceError(f"required native test did not pass: {name}")
        proven.extend(matches)
    return proven


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"JSON evidence must be an object: {path}")
    return payload


def _require_fields(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    payload = _load_json(path)
    for key, value in expected.items():
        if payload.get(key) != value:
            raise EvidenceError(
                f"{path.name} field {key!r} was {payload.get(key)!r}, expected {value!r}"
            )
    return payload


def _run(argv: list[str], *, cwd: Path, log: Path) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    output = completed.stdout + completed.stderr
    log.write_text(output, encoding="utf-8")
    if completed.returncode != 0:
        raise EvidenceError(
            f"command failed with {completed.returncode}: {argv!r}; see {log.name}"
        )
    return output


def _secret_values(auth_path: Path) -> set[str]:
    if not auth_path.is_file():
        raise EvidenceError(f"operator Codex auth file is unavailable: {auth_path}")
    payload = _load_json(auth_path)
    values: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif (
            isinstance(value, str)
            and len(value) >= 16
            and any(marker in key.casefold() for marker in ("token", "key", "secret"))
        ):
            values.add(value)

    visit(payload)
    if not values:
        raise EvidenceError("operator Codex auth file contained no auditable secret values")
    return values


def _count_secret_occurrences(root: Path, secrets: set[str]) -> int:
    count = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise EvidenceError(f"cannot scan evidence file for secrets: {path}: {exc}") from exc
        if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = payload.decode("utf-16", errors="replace")
        else:
            try:
                text = payload.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = payload.decode("cp1252", errors="replace")
        count += sum(text.count(secret) for secret in secrets)
    return count


def _write_result(
    evidence_root: Path,
    name: str,
    payload: dict[str, Any],
    tests: list[str],
    *,
    probes: Mapping[str, Any] | None = None,
) -> None:
    result = dict(payload)
    result["evidence"] = {
        "passed_native_tests": sorted(set(tests)),
        "probes": dict(probes or {}),
    }
    target = evidence_root / name
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def produce(args: argparse.Namespace) -> None:
    if sys.platform != "win32":
        raise EvidenceError("local acceptance evidence must be produced on native Windows")

    source_root = args.source_root.resolve()
    evidence_root = args.evidence_root.resolve()
    fixture_root = args.fixture_root.resolve()
    revision = args.source_revision.lower()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise EvidenceError("source revision must be an exact 40-character SHA")
    for name in (*LOCAL_ARTIFACTS, "lab-controller-static-result.json"):
        (evidence_root / name).unlink(missing_ok=True)

    passed = passed_tests(args.native_junit, args.matrix_junit)
    tests_by_artifact = {
        name: require_tests(passed, required)
        for name, required in REQUIRED_TESTS.items()
    }
    lab_controller_tests = require_tests(
        passed,
        (
            "test_windows_lab_has_complete_controller_surface",
            "test_windows_lab_inputs_are_placeholder_only_and_private_state_is_ignored",
            "test_windows_lab_templates_render_only_into_ignored_state",
            "test_windows_lab_contains_no_checked_in_identity_or_common_secret_shape",
        ),
    )

    lifecycle = _require_fields(
        evidence_root / "lifecycle-result.json",
        {
            "status": "passed",
            "source_revision": revision,
            "provider": "codex",
            "pull_request_merged": True,
            "provenance_tag_present": True,
            "review_decision": "approved",
            "review_bootstrap_warning_present": False,
            "implementation_workspaces_remaining": 0,
        },
    )
    web_lifecycle = _require_fields(
        evidence_root / "web-lifecycle-result.json",
        {
            "status": "passed",
            "foreground": "passed",
            "background": "passed",
            "authenticated_readiness": True,
            "remaining_server_and_action_processes": 0,
        },
    )
    web_chat = _require_fields(
        evidence_root / "web-chat-result.json",
        {
            "status": "passed",
            "source_revision": revision,
            "backend": "codex",
            "dependent_turns": 3,
            "turn_2_retained_turn_1": True,
            "turn_3_retained_turns_1_and_2": True,
            "reconnect_endpoint_exercised": True,
            "concurrent_sessions_isolated": True,
            "cancelled_descendants_remaining": 0,
        },
    )

    git = shutil.which("git.exe") or shutil.which("git")
    gh = shutil.which("gh.exe") or shutil.which("gh")
    if not git or not gh:
        raise EvidenceError("Git and GitHub CLI must both resolve through PATH")
    _run([git, "--version"], cwd=fixture_root, log=evidence_root / "path-git-version.log")
    _run([gh, "--version"], cwd=fixture_root, log=evidence_root / "path-gh-version.log")
    if not fixture_root.drive:
        raise EvidenceError("fixture repository is not on a drive-letter path")
    fixture_text = str(fixture_root)
    if " " not in fixture_text or "\\" not in fixture_text:
        raise EvidenceError("fixture path did not exercise native backslashes and spaces")
    with tempfile.TemporaryDirectory(prefix="SpecCaseProbe-雪 ", dir=fixture_root) as probe_name:
        probe = Path(probe_name)
        if "雪" not in str(probe):
            raise EvidenceError("path probe did not exercise non-CP1252 Unicode")
        mixed = probe / "MiXeD-Case.txt"
        mixed.write_text("case probe", encoding="utf-8")
        if not (probe / "mixed-case.TXT").is_file():
            raise EvidenceError("fixture filesystem did not behave case-insensitively")

    docs = {
        "README": source_root / "README.md",
        "INSTALL": source_root / "INSTALL.md",
        "troubleshooting": source_root / "docs" / "windows.md",
        "support-matrix": source_root / "docs" / "windows.md",
    }
    doc_text = {name: path.read_text(encoding="utf-8") for name, path in docs.items()}
    combined_docs = "\n".join(doc_text.values())
    for required in (
        "Windows 11",
        "local fixed NTFS",
        "worktree",
        "Codex",
        "Windows PowerShell",
        "Native Claude is unavailable",
        "Docker Desktop",
        "UNC/network",
    ):
        if required not in combined_docs:
            raise EvidenceError(f"Windows documentation is missing: {required}")
    alternatives = sum(
        phrase in doc_text["support-matrix"]
        for phrase in ("WSL2", "Linux container")
    )
    if alternatives < 1:
        raise EvidenceError("native Claude documentation has no supported alternative")

    pyproject = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = pyproject.get("project", {}).get("classifiers", [])
    windows_classifier = "Operating System :: Microsoft :: Windows" in classifiers
    if not windows_classifier:
        raise EvidenceError("package metadata has no Windows classifier")
    for label, python in (("wheel", args.wheel_python), ("sdist", args.sdist_python)):
        _run(
            [str(python), "-I", "-m", "spec_runtime.cli", "--version"],
            cwd=fixture_root,
            log=evidence_root / f"package-{label}-version.log",
        )
        _run(
            [str(python), "-I", "-m", "pip", "check"],
            cwd=fixture_root,
            log=evidence_root / f"package-{label}-pip-check.log",
        )
    doctor_output = _run(
        [str(args.wheel_python), "-I", "-m", "spec_runtime.cli", "doctor", "--repo-root", str(fixture_root)],
        cwd=fixture_root,
        log=evidence_root / "package-doctor.log",
    ).lower()
    if "0 blocker(s)" not in doctor_output or "0 warning(s)" not in doctor_output:
        raise EvidenceError("documented Windows fixture did not pass doctor without warnings")
    watch_output = _run(
        [str(args.wheel_python), "-I", "-m", "spec_runtime.cli", "watch", "--repo-root", str(fixture_root)],
        cwd=fixture_root,
        log=evidence_root / "watch-noninteractive.log",
    )
    if not watch_output.strip():
        raise EvidenceError("non-interactive watch produced no explicit terminal output")

    secrets = _secret_values(args.operator_codex_home / "auth.json")
    auth_remnants = list(fixture_root.rglob("auth.json"))
    review_workspaces = [
        path
        for path in (fixture_root / ".worktrees").glob("*")
        if "review" in path.name or "block-debugger" in path.name
    ] if (fixture_root / ".worktrees").is_dir() else []
    secret_occurrences = {
        "cleanup_remnants": len(auth_remnants),
        "commits": 0,
        "logs": _count_secret_occurrences(evidence_root, secrets),
        "review_workspaces": sum(_count_secret_occurrences(path, secrets) for path in review_workspaces),
    }
    # Avoid shell expansion in the commit scan. Enumerate refs and inspect each
    # through Git's argv boundary instead.
    refs = subprocess.run(
        [git, "for-each-ref", "--format=%(refname)"],
        cwd=fixture_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.splitlines()
    commit_hits = 0
    for ref in refs:
        patterns = ["(OPENAI_API_KEY|access_token|refresh_token)", *sorted(secrets)]
        for index, pattern in enumerate(patterns):
            mode = "-E" if index == 0 else "-F"
            completed = subprocess.run(
                [git, "grep", "-I", mode, "-e", pattern, ref],
                cwd=fixture_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode not in (0, 1):
                raise EvidenceError(f"Git secret scan failed for {ref}")
            commit_hits += len(
                [line for line in completed.stdout.splitlines() if line.strip()]
            )
    secret_occurrences["commits"] = commit_hits
    (evidence_root / "isolation-commit-scan.log").write_text(
        f"refs_scanned={len(refs)}\nsecret_shape_hits={commit_hits}\n",
        encoding="utf-8",
    )
    if any(secret_occurrences.values()) or review_workspaces:
        raise EvidenceError(
            f"secret or isolated-workspace cleanup probe failed: {secret_occurrences}; "
            f"review_workspaces={len(review_workspaces)}"
        )

    web_tracebacks = 0
    for log in evidence_root.glob("web*.log"):
        web_tracebacks += log.read_text(
            encoding="utf-8", errors="ignore"
        ).count("Traceback (most recent call last)")
    if web_tracebacks:
        raise EvidenceError(f"web runtime logs contained {web_tracebacks} Python tracebacks")

    commands = [
        "create", "task", "implement", "phase", "report", "input",
        "status", "stop", "clean", "gc", "update",
    ]
    _write_result(
        evidence_root,
        "native-command-matrix-result.json",
        {"status": "passed", "source_revision": revision, "commands": commands, "backend": "worktree"},
        tests_by_artifact["native-command-matrix-result.json"],
        probes={"real_lifecycle": lifecycle["status"], "installed_wheel_matrix": "passed"},
    )
    _write_result(
        evidence_root,
        "lifecycle-fault-matrix-result.json",
        {
            "status": "passed",
            "source_revision": revision,
            "scenarios": ["stop", "timeout", "failed-gate", "review-retry", "orchestrator-restart", "stale-state", "merge-conflict", "needs-input"],
            "owned_processes_remaining": 0,
            "unrelated_process_preserved": True,
        },
        tests_by_artifact["lifecycle-fault-matrix-result.json"],
    )
    _write_result(
        evidence_root,
        "isolation-result.json",
        {"status": "passed", "source_revision": revision, "requires_symlink_privilege": False, "requires_developer_mode": False, "secret_occurrences": secret_occurrences},
        tests_by_artifact["isolation-result.json"],
        probes={"auth_remnants": 0, "review_workspaces_remaining": 0, "refs_scanned": len(refs)},
    )
    _write_result(
        evidence_root,
        "windows-path-result.json",
        {
            "status": "passed",
            "source_revision": revision,
            "discovery": {"gh": True, "git": True},
            "cases": {"backslashes": True, "case_insensitive": True, "crlf": True, "drive_letter": True, "spaces": True, "unicode_non_cp1252": True},
        },
        tests_by_artifact["windows-path-result.json"],
        probes={"fixture_path": fixture_text, "git_executable": git, "gh_executable": gh},
    )
    _write_result(
        evidence_root,
        "review-isolation-result.json",
        {"status": "passed", "source_revision": revision, "reviewer": "isolated", "block_debugger": "isolated", "file_attributes_claimed_as_boundary": False, "failures_actionable": True},
        tests_by_artifact["review-isolation-result.json"],
        probes={"real_review_decision": lifecycle["review_decision"], "bootstrap_fallback": False},
    )
    _write_result(
        evidence_root,
        "native-claude-result.json",
        {"status": "passed", "source_revision": revision, "failed_before_launch": True, "provider_processes_started": 0, "documented_alternatives": alternatives, "sandbox_policy_weakened": False},
        tests_by_artifact["native-claude-result.json"],
    )
    _write_result(
        evidence_root,
        "update-result.json",
        {"status": "passed", "source_revision": revision, "foreground_applied": True, "background_survived_cli_exit": True, "durable_identity_recorded": True, "remaining_processes_and_state": 0},
        tests_by_artifact["update-result.json"],
    )
    _write_result(
        evidence_root,
        "test-coverage-result.json",
        {"status": "passed", "source_revision": revision, "hermetic_lifecycle": True, "real_provider_separately_marked": True},
        tests_by_artifact["test-coverage-result.json"],
        probes={"real_provider_lifecycle": lifecycle["status"]},
    )
    _write_result(
        evidence_root,
        "web-action-result.json",
        {"status": "passed", "source_revision": revision, "shared_runtime": True, "shared_process_supervision": True, "structured_failure": True, "posix_tracebacks": web_tracebacks},
        tests_by_artifact["web-action-result.json"],
        probes={"real_web_lifecycle": web_lifecycle["status"]},
    )
    _write_result(
        evidence_root,
        "watch-result.json",
        {"status": "passed", "source_revision": revision, "chat": "passed", "portable_process_inspection": True, "portable_memory_inspection": True, "terminal_degradation": "explicit", "dispatcher_crashed": False},
        tests_by_artifact["watch-result.json"],
        probes={"noninteractive_output_bytes": len(watch_output.encode("utf-8"))},
    )
    _write_result(
        evidence_root,
        "web-integration-result.json",
        {
            "status": "passed",
            "source_revision": revision,
            "real_subprocesses_and_listener": True,
            "cases": ["authentication", "reconnect", "concurrent-chats", "context", "cancellation", "foreground-start-stop", "background-start-stop", "stale-server-identity"],
        },
        tests_by_artifact["web-integration-result.json"],
        probes={"real_web_lifecycle": web_lifecycle["status"], "real_web_chat": web_chat["status"]},
    )
    _write_result(
        evidence_root,
        "documentation-audit-result.json",
        {"status": "passed", "source_revision": revision, "surfaces": ["README", "INSTALL", "troubleshooting", "support-matrix"], "exact_supported_tier": True, "native_codex_claude_container_distinguished": True},
        tests_by_artifact["documentation-audit-result.json"],
        probes={"files": sorted({str(path.relative_to(source_root)) for path in docs.values()})},
    )
    _write_result(
        evidence_root,
        "package-release-result.json",
        {"status": "passed", "source_revision": revision, "windows_classifier": True, "wheel_install": "passed", "sdist_install": "passed", "doctor_unexplained_warnings": 0},
        tests_by_artifact["package-release-result.json"],
        probes={"wheel_python": str(args.wheel_python), "sdist_python": str(args.sdist_python)},
    )
    _write_result(
        evidence_root,
        "lab-controller-static-result.json",
        {
            "status": "passed",
            "source_revision": revision,
            "checked_in_secret_free": True,
            "operations": ["create", "reset", "source-sync", "exec", "collect", "snapshot"],
            "machine_state_outside_git": True,
        },
        lab_controller_tests,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--native-junit", type=Path, required=True)
    parser.add_argument("--matrix-junit", type=Path, required=True)
    parser.add_argument("--wheel-python", type=Path, required=True)
    parser.add_argument("--sdist-python", type=Path, required=True)
    parser.add_argument("--operator-codex-home", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        produce(build_parser().parse_args(argv))
    except (EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"local Windows acceptance evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
