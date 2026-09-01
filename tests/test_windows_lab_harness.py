from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
LAB_ROOT = REPO_ROOT / "tools" / "windows-lab"
WINDOWS_DOC = REPO_ROOT / "docs" / "windows.md"

REQUIRED_FILES = {
    ".gitignore",
    "acceptance-manifest.json",
    "audit_acceptance.py",
    "Autounattend.xml.template",
    "autopilot-agent.cs",
    "Dockerfile",
    "README.md",
    "bootstrap.ps1.template",
    "compose.yaml",
    "entrypoint.sh",
    "import_external_evidence.py",
    "job-runner.ps1",
    "lab.env.example",
    "labctl",
    "local_acceptance.py",
    "proof.ps1",
    "provision.ps1",
    "redact.py",
    "register-job.ps1",
    "runtime_proof.py",
    "toolchain.json.example",
    "trash_retention.py",
    "watch-conpty.cs",
}


def _harness_text() -> str:
    return "\n".join(
        (LAB_ROOT / name).read_text(encoding="utf-8")
        for name in sorted(REQUIRED_FILES)
    )


def test_windows_lab_has_complete_controller_surface() -> None:
    assert REQUIRED_FILES <= {path.name for path in LAB_ROOT.iterdir() if path.is_file()}
    controller = (LAB_ROOT / "labctl").read_text(encoding="utf-8")
    for command in (
        "init",
        "up",
        "down",
        "shutdown",
        "wait",
        "provision",
        "snapshot",
        "reset",
        "stage",
        "exec",
        "job",
        "collect",
        "proof",
        "logs",
        "status",
        "ssh",
    ):
        assert re.search(rf"(^|[| ]){re.escape(command)}([| )])", controller, re.MULTILINE)

    assert "reset_lab \"$LAB_BASELINE\"" in controller
    assert 'require_free_space "$STATE_ROOT"' in controller
    assert 'local timeout="${LAB_SHUTDOWN_TIMEOUT_SECONDS:-600}"' in controller
    assert 'vm_running || break' in controller
    assert 'local keep="${LAB_PROOF_TRASH_KEEP:-1}"' in controller
    assert 'trash_keep="$(proof_trash_keep)"' in controller
    assert 'proof_trash_retention "$trash_keep" apply' in controller
    assert "provision_guest" in controller
    assert "stage_source" in controller
    assert 'mkdir -p "$STATE_ROOT/incoming"' in controller
    assert "job_submit" in controller
    assert "job_wait" in controller
    assert "collect_artifacts" in controller
    assert 'STATE_ROOT="${SPEC_WINDOWS_LAB_STATE_ROOT:-$LAB_ROOT/state}"' in controller
    assert 'SPEC_WINDOWS_LAB_STATE_ROOT="$STATE_ROOT"' in controller
    proof = (LAB_ROOT / "proof.ps1").read_text(encoding="utf-8")
    assert "-m', 'pytest'" in proof
    assert "'repo', 'create'" in proof
    assert "'implement', '--spec', 'add-numbers'" in proof
    assert "Write-Utf8NoBom" in proof
    assert "runtime_proof.py" in proof
    assert "dependent_turns" in (LAB_ROOT / "runtime_proof.py").read_text(encoding="utf-8")
    assert "autopilot-adopted-state.json" in proof
    assert "adoption_generation -ne 1" in proof
    assert "blocked_dependent_dispatch_count = 0" in proof
    assert "watch-conpty.cs" in proof
    assert "watch-interactive-result.json" in proof
    assert "runtime.watch-conpty-chat" in proof
    assert "'/platform:x64'" in proof
    watch_harness = (LAB_ROOT / "watch-conpty.cs").read_text(encoding="utf-8")
    for native_boundary in (
        "CreatePseudoConsole",
        "PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE",
        "STARTF_USESTDHANDLES",
        "venv-python--isolated-module",
        "watch-interactive-failed-transcript.log",
        "WaitForProviderExit",
        'inputWriter.Write("q")',
        "KILL_ON_JOB_CLOSE",
        "CREATE_SUSPENDED",
        "ResumeThread",
        "TerminateJobObject",
        "watch-interactive-failure.json",
    ):
        assert native_boundary in watch_harness
    main = watch_harness[watch_harness.index("public static int Main") :]
    assert (
        main.index("job = CreateJobObject")
        < main.index("CreateProcess(")
        < main.index("AssignProcessToJobObject")
        < main.index("ResumeThread(")
    )
    graceful = main[main.index("// q must end the complete app tree") :]
    assert (
        graceful.index("WaitForObservedExit(15, true)")
        < graceful.index("ClosePseudoConsole(pseudoConsole)")
        < graceful.index("CloseHandle(empty watch Job)")
    )
    emergency = watch_harness[
        watch_harness.index("private static void EmergencyCleanup") :
        watch_harness.index("private static string JsonEscape")
    ]
    assert (
        emergency.index("TerminateJobObject")
        < emergency.index("TerminateProcess")
        < emergency.index("ClosePseudoConsole(pseudoConsole);")
    )
    assert "Set-EvidenceClaim" in proof
    assert "Proof must run with a non-elevated user token" in proof
    assert "Get-CimInstance -ClassName Win32_OperatingSystem" in proof
    assert "$windowsBuildNumber -lt 22000" in proof
    assert "$windowsProductType -ne 1" in proof
    assert "windows_build_number = $windowsBuildNumber" in proof
    assert "windows_product_type = $windowsProductType" in proof
    assert "$windowsProduct -notmatch 'Windows 11'" not in proof
    register_job = (LAB_ROOT / "register-job.ps1").read_text(encoding="utf-8")
    assert "-RunLevel Limited" in register_job
    assert "-RunLevel Highest" not in register_job
    provision = (LAB_ROOT / "provision.ps1").read_text(encoding="utf-8")
    assert 'icacls.exe $harnessRoot /grant:r "${account}:(OI)(CI)M" /T /C' in provision
    assert "-Filter 'codex-code-mode-host-*.exe'" in provision
    assert "$codexHostAlias = Join-Path $codexRoot 'codex-code-mode-host.exe'" in provision
    assert "$codexHostSourceHash -ne $codexHostAliasHash" in provision
    assert "Codex code-mode host is missing from its required canonical path" in proof
    runner = (LAB_ROOT / "job-runner.ps1").read_text(encoding="utf-8")
    assert '$env:TEMP = $temp' in runner
    assert '$env:TMP = $temp' in runner
    assert 'python3 "$LAB_ROOT/audit_acceptance.py"' in controller
    assert '--expected-revision "$revision"' in controller
    assert '--output "$destination/acceptance-audit.json"' in controller
    assert 'return "$audit_status"' in controller

    reset = controller[controller.index("reset_lab() {") : controller.index("stage_source() {")]
    assert reset.index("qemu_img check /state/disk/run.qcow2") < reset.index(
        'printf \'%s\\n\' "Reset run overlay'
    )
    retention = controller[
        controller.index("proof_trash_retention() {") : controller.index("stage_source() {")
    ]
    assert "require_stopped" in retention
    env_example = (LAB_ROOT / "lab.env.example").read_text(encoding="utf-8")
    assert "LAB_SHUTDOWN_TIMEOUT_SECONDS=600" in env_example
    assert "LAB_PROOF_TRASH_KEEP=1" in env_example
    proof_run = controller[
        controller.index("run_proof() {") : controller.index('command="${1:-help}"')
    ]
    assert (
        proof_run.index("shutdown_lab")
        < proof_run.index('proof_trash_retention "$trash_keep"')
        < proof_run.index('reset_lab "$LAB_BASELINE"')
        < proof_run.index('proof_trash_retention "$trash_keep" apply')
        < proof_run.index('require_free_space "$STATE_ROOT"')
        < proof_run.index("up_lab")
    )


def test_windows_lab_inputs_are_placeholder_only_and_private_state_is_ignored() -> None:
    env_example = (LAB_ROOT / "lab.env.example").read_text(encoding="utf-8")
    toolchain = json.loads((LAB_ROOT / "toolchain.json.example").read_text(encoding="utf-8"))
    ignored = set((LAB_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    assert "<windows-iso-sha256>" in env_example
    assert "<github-owner>" in env_example
    assert set(toolchain) == {
        "git",
        "gh",
        "uv",
        "codex",
        "codex_code_mode_host",
        "codex_sandbox_setup",
    }
    for entry in toolchain.values():
        assert entry["url"].startswith("<https-url-")
        assert entry["sha256"].startswith("<")
        assert Path(entry["filename"]).name == entry["filename"]
    assert {"lab.env", "toolchain.json", "state/", "artifacts/", "*.iso", "*.qcow2"} <= ignored
    if (REPO_ROOT / ".git").exists():
        for relative in (
            "tools/windows-lab/lab.env",
            "tools/windows-lab/toolchain.json",
            "tools/windows-lab/state/identity.env",
            "tools/windows-lab/artifacts/proof/result.json",
            "tools/windows-lab/windows.iso",
            "tools/windows-lab/run.qcow2",
        ):
            result = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", relative],
                cwd=REPO_ROOT,
                check=False,
            )
            assert result.returncode == 0, relative


def test_windows_lab_templates_render_only_into_ignored_state() -> None:
    controller = (LAB_ROOT / "labctl").read_text(encoding="utf-8")
    unattended = (LAB_ROOT / "Autounattend.xml.template").read_text(encoding="utf-8")
    bootstrap = (LAB_ROOT / "bootstrap.ps1.template").read_text(encoding="utf-8")

    assert "__ADMIN_PASSWORD__" in unattended
    assert "__WINDOWS_IMAGE_INDEX__" in unattended
    assert "__SSH_PUBLIC_KEY__" in bootstrap
    ET.fromstring(unattended)
    assert '"$STATE_ROOT/unattend/Autounattend.xml"' in controller
    assert '"$STATE_ROOT/unattend/bootstrap.ps1"' in controller
    assert 'chmod 0600 "$STATE_ROOT/unattend/Autounattend.xml"' in controller


def test_windows_lab_contains_no_checked_in_identity_or_common_secret_shape() -> None:
    text = _harness_text()
    assert not re.search(r"/home/[A-Za-z0-9._-]+/", text)
    assert not re.search(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", text)
    assert "BEGIN OPENSSH PRIVATE KEY" not in text
    assert not re.search(r"github_pat_[A-Za-z0-9_]{20,}", text)
    assert not re.search(r"gh[opsu]_[A-Za-z0-9]{20,}", text)
    assert not re.search(r"sk-[A-Za-z0-9_-]{20,}", text)


def test_windows_lab_redactor_removes_supported_secret_shapes(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    destination = tmp_path / "sanitized"
    source.mkdir()
    token = "ghp_" + "A" * 36
    web_token = "web-auth-token-" + "Z" * 32
    (source / "proof.log").write_text(
        f"Authorization: Bearer {token}\n"
        f"Authenticated URL: http://127.0.0.1:17702/?token={web_token}\n"
    )

    subprocess.run(
        [sys.executable, str(LAB_ROOT / "redact.py"), str(source), str(destination)],
        check=True,
    )

    redacted = (destination / "proof.log").read_text()
    assert token not in redacted
    assert web_token not in redacted
    assert "[REDACTED]" in redacted

    utf16_token = "eyJ" + "B" * 32
    (source / "powershell.log").write_text(
        json.dumps({"access_token": utf16_token}) + "\n", encoding="utf-16"
    )
    revision = "a" * 40
    subprocess.run(
        [
            sys.executable,
            str(LAB_ROOT / "redact.py"),
            str(source),
            str(destination),
            revision,
        ],
        check=True,
    )
    utf16_redacted = (destination / "powershell.log").read_text(encoding="utf-16")
    assert utf16_token not in utf16_redacted
    assert "[REDACTED]" in utf16_redacted
    report = json.loads((destination / "_redaction-report.json").read_text())
    assert report["status"] == "passed"
    assert report["source_revision"] == revision
    assert report["files_processed"] == 2
    assert report["files_with_replacements"] == 2
    assert report["recognized_secret_shapes_remaining"] == []


def test_windows_runtime_proof_sse_parser_is_hermetic() -> None:
    path = LAB_ROOT / "runtime_proof.py"
    spec = importlib.util.spec_from_file_location("windows_runtime_proof", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    events = module.parse_sse(
        [
            b": keepalive\r\n",
            b"\r\n",
            b"event: agent_event\r\n",
            b'data: {"kind":"text","text":"first"}\r\n',
            b"\r\n",
            b"event: agent_event\n",
            b'data: {"kind":"done"}\n',
            b"\n",
        ]
    )

    assert events == [
        {"kind": "text", "text": "first"},
        {"kind": "done"},
    ]


def test_windows_runtime_proof_declares_real_runtime_invariants() -> None:
    runtime = (LAB_ROOT / "runtime_proof.py").read_text(encoding="utf-8")
    proof = (LAB_ROOT / "proof.ps1").read_text(encoding="utf-8")
    agent = (LAB_ROOT / "autopilot-agent.cs").read_text(encoding="utf-8")
    runner = (LAB_ROOT / "job-runner.ps1").read_text(encoding="utf-8")

    for statement in (
        "dependent_turns",
        "reconnect_endpoint_exercised",
        "reconnect_replayed_event_count",
        "concurrent_sessions_isolated",
        "cancelled_descendants_remaining",
        "native_claude_failed_closed",
        "_sse_reconnect",
        "_tree_identities",
    ):
        assert statement in runtime
    for statement in (
        "runtime.web-chat",
        "runtime.autopilot",
        "runtime.timeout-cleanup",
        "Stop-Process -Id $firstDispatcher.Id -Force",
        "spec auto stop",
        "autopilot-result.json",
    ):
        assert statement in proof
    assert "SPEC_AUTOPILOT_PROOF_RELEASE" in agent
    assert "report --status needs-input" in agent
    assert "$ErrorActionPreference = 'Continue'" in runner
    assert "$exitCode = if ($null -eq $LASTEXITCODE)" in runner


def test_windows_local_acceptance_requires_executed_unskipped_tests(tmp_path: Path) -> None:
    path = LAB_ROOT / "local_acceptance.py"
    spec = importlib.util.spec_from_file_location("windows_local_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    report = tmp_path / "report.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite tests="2" failures="0" errors="0" skipped="1">
  <testcase classname="proof" name="passed_case" />
  <testcase classname="proof" name="skipped_case"><skipped /></testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    passed = module.passed_tests(report)
    assert passed == {"passed_case"}
    assert module.require_tests(passed, ("passed_case",)) == ["passed_case"]
    with pytest.raises(module.EvidenceError, match="did not pass"):
        module.require_tests(passed, ("skipped_case",))

    failed_report = tmp_path / "failed.xml"
    failed_report.write_text(
        """<testsuite tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="proof" name="failed_case"><failure>boom</failure></testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    with pytest.raises(module.EvidenceError, match="not green"):
        module.passed_tests(failed_report)

    secret = "utf16-secret-value-1234567890"
    secret_log = tmp_path / "powershell.log"
    secret_log.write_text(f"token={secret}\n", encoding="utf-16")
    assert module._count_secret_occurrences(tmp_path, {secret}) == 1


def test_windows_local_acceptance_requires_real_retained_conpty_watch(
    tmp_path: Path,
) -> None:
    path = LAB_ROOT / "local_acceptance.py"
    spec = importlib.util.spec_from_file_location("windows_watch_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    revision = "a" * 40
    wheel_scripts = tmp_path / "wheel" / "Scripts"
    wheel_scripts.mkdir(parents=True)
    executable = wheel_scripts / "spec.exe"
    executable.write_bytes(b"installed wheel launcher")
    transcript = tmp_path / "watch-interactive-transcript.log"
    marker = "SPEC_WATCH_CODEX_" + "b" * 16
    transcript.write_text(
        f"Specs Queue auto-root-a needs-input phase=implement branch=code/example "
        f"auto-root-a chat  agent=codex {marker}",
        encoding="utf-8",
    )
    result = tmp_path / "watch-interactive-result.json"
    payload = {
        "status": "passed",
        "source_revision": revision,
        "platform": "win32",
        "pseudoconsole": "ConPTY",
        "installed_artifact": True,
        "launch_boundary": "venv-python--isolated-module",
        "spec_executable": str(executable),
        "interactive_desktop": True,
        "session_id": 1,
        "terminal_columns": 180,
        "terminal_rows": 50,
        "dashboard_observed": True,
        "selected_spec": "auto-root-a",
        "live_status_observed": "needs-input",
        "detail_observed": True,
        "chat_screen_observed": True,
        "chat_provider": "codex",
        "codex_provider_process_observed": True,
        "provider_identity": {
            "pid": 1234,
            "start_time_utc_ticks": 638900000000000000,
            "name": "codex.exe",
        },
        "expected_marker": marker,
        "observed_marker": marker,
        "marker_matched": True,
        "quit_key": "q",
        "root_exit_code": 0,
        "root_created_suspended": True,
        "job_assigned_before_resume": True,
        "root_resumed": True,
        "graceful_cleanup_observed": True,
        "graceful_owned_processes_remaining": 0,
        "emergency_cleanup_invoked": False,
        "provider_processes_remaining": 0,
        "dispatcher_processes_remaining": 0,
        "owned_processes_remaining": 0,
        "observed_descendant_count": 2,
        "transcript_file": transcript.name,
        "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
    }
    result.write_text(json.dumps(payload), encoding="utf-8")

    validated = module.validate_interactive_watch_evidence(
        result,
        revision=revision,
        expected_spec_executable=executable,
    )
    assert validated["expected_marker"] == marker

    failure = tmp_path / "watch-interactive-failure.json"
    failure.write_text('{"status":"failed"}', encoding="utf-8")
    with pytest.raises(module.EvidenceError, match="contradictory emergency-cleanup"):
        module.validate_interactive_watch_evidence(
            result,
            revision=revision,
            expected_spec_executable=executable,
        )
    failure.unlink()

    payload["marker_matched"] = False
    result.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.EvidenceError, match="marker_matched"):
        module.validate_interactive_watch_evidence(
            result,
            revision=revision,
            expected_spec_executable=executable,
        )

    payload["marker_matched"] = True
    payload["emergency_cleanup_invoked"] = True
    result.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.EvidenceError, match="emergency_cleanup_invoked"):
        module.validate_interactive_watch_evidence(
            result,
            revision=revision,
            expected_spec_executable=executable,
        )

    payload["emergency_cleanup_invoked"] = False
    payload["transcript_sha256"] = "0" * 64
    result.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.EvidenceError, match="hash does not match"):
        module.validate_interactive_watch_evidence(
            result,
            revision=revision,
            expected_spec_executable=executable,
        )


def test_windows_local_acceptance_covers_every_local_manifest_result() -> None:
    path = LAB_ROOT / "local_acceptance.py"
    spec = importlib.util.spec_from_file_location("windows_local_acceptance_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = json.loads((LAB_ROOT / "acceptance-manifest.json").read_text(encoding="utf-8"))
    manifest_artifacts = {
        check["artifact"]
        for criterion in manifest["criteria"]
        for check in criterion["checks"]
        if "artifact" in check
    }

    assert set(module.LOCAL_ARTIFACTS) <= manifest_artifacts
    assert set(module.EXTERNAL_ARTIFACTS) <= manifest_artifacts
    assert set(module.LOCAL_ARTIFACTS) == set(module.REQUIRED_TESTS)

    proof = (LAB_ROOT / "proof.ps1").read_text(encoding="utf-8")
    for artifact in module.LOCAL_ARTIFACTS:
        assert artifact in proof or artifact == "package-release-result.json"
    assert "installed-cli-matrix.junit.xml" in proof
    assert "SPEC_WINDOWS_INSTALLED_CLI_MATRIX = '1'" in proof
    assert "'-o', 'pythonpath=', '--import-mode=importlib'" in proof
    assert "'--wheel', '--sdist'" in proof
    assert "local_acceptance.py" in proof

    controller = (LAB_ROOT / "labctl").read_text(encoding="utf-8")
    assert "lab-controller-static-result.json" in controller
    assert "lab-controller-result.json" in controller
    assert "clean_snapshot_reset" in controller
    assert "if (( proof_status != 0 ))" in controller
    assert "controller success evidence will not be produced" in controller


def test_external_evidence_import_requires_one_exact_revision_bundle(tmp_path: Path) -> None:
    source = tmp_path / "external"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    revision = "a" * 40
    (destination / "_redaction-report.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "source_revision": revision,
                "files_processed": 10,
                "text_files_scanned": 10,
                "files_with_replacements": 0,
                "recognized_secret_shapes_remaining": [],
            }
        ),
        encoding="utf-8",
    )
    github_run = {
        "repository": "specbutler/specbutler",
        "workflow": "ci",
        "run_id": 123,
        "run_attempt": 1,
        "event_name": "push",
        "github_sha": revision,
    }
    hosted = (
        "cross-platform-lifecycle-result.json",
        "cross-platform-web-result.json",
        "hosted-windows-ci-result.json",
        "hosted-windows-smoke-result.json",
    )
    (source / "hosted-ci-evidence-index.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "source_revision": revision,
                "github_run": github_run,
                "reports": list(hosted),
            }
        ),
        encoding="utf-8",
    )
    for name in hosted:
        (source / name).write_text(
            json.dumps(
                {
                    "status": "passed",
                    "source_revision": revision,
                    "github_run": github_run,
                }
            ),
            encoding="utf-8",
        )
    (source / "linux-claude-web-result.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "source_revision": revision,
                "backend": "claude",
                "dependent_turns": 3,
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(LAB_ROOT / "import_external_evidence.py"),
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--expected-revision",
            revision,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name for path in destination.iterdir()} == {
        *hosted,
        "linux-claude-web-result.json",
        "external-evidence-import.json",
        "_redaction-report.json",
    }
    report = json.loads((destination / "external-evidence-import.json").read_text())
    assert report["source_revision"] == revision
    assert set(report["imported_sha256"]) == {*hosted, "linux-claude-web-result.json"}
    redaction = json.loads((destination / "_redaction-report.json").read_text())
    assert redaction["files_processed"] == 15
    assert redaction["external_results_scanned"] == [*hosted, "linux-claude-web-result.json"]

    stale_destination = tmp_path / "stale-destination"
    stale_destination.mkdir()
    (stale_destination / "_redaction-report.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "source_revision": "b" * 40,
                "recognized_secret_shapes_remaining": [],
            }
        ),
        encoding="utf-8",
    )
    stale_result = subprocess.run(
        [
            sys.executable,
            str(LAB_ROOT / "import_external_evidence.py"),
            "--source",
            str(source),
            "--destination",
            str(stale_destination),
            "--expected-revision",
            "b" * 40,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stale_result.returncode == 1
    assert {path.name for path in stale_destination.glob("*.json")} == {
        "_redaction-report.json"
    }

    secret_payload = json.loads((source / "linux-claude-web-result.json").read_text())
    secret_payload["access_token"] = "credential-value-that-must-not-be-imported"
    (source / "linux-claude-web-result.json").write_text(
        json.dumps(secret_payload), encoding="utf-8"
    )
    secret_destination = tmp_path / "secret-destination"
    secret_destination.mkdir()
    (secret_destination / "_redaction-report.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "source_revision": revision,
                "recognized_secret_shapes_remaining": [],
            }
        ),
        encoding="utf-8",
    )
    secret_result = subprocess.run(
        [
            sys.executable,
            str(LAB_ROOT / "import_external_evidence.py"),
            "--source",
            str(source),
            "--destination",
            str(secret_destination),
            "--expected-revision",
            revision,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert secret_result.returncode == 1
    assert "credential shape" in secret_result.stdout
    assert not (secret_destination / "linux-claude-web-result.json").exists()


def test_windows_runtime_timeout_probe_kills_a_real_tree(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    subprocess.run(
        [
            sys.executable,
            str(LAB_ROOT / "runtime_proof.py"),
            "timeout-tree",
            "--work-root",
            str(tmp_path / "work"),
            "--evidence-root",
            str(evidence),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=True,
        timeout=20,
    )

    result = json.loads((evidence / "timeout-tree-result.json").read_text())
    assert result == {
        "processes_remaining": [],
        "status": "passed",
        "timeout_observed": True,
        "tree_depth": 3,
    }


def test_windows_lab_scripts_parse_and_compose_is_loopback_only() -> None:
    if os.name != "nt":
        bash = shutil.which("bash")
        shell = shutil.which("sh")
        assert bash and shell
        subprocess.run([bash, "-n", str(LAB_ROOT / "labctl")], check=True)
        subprocess.run([shell, "-n", str(LAB_ROOT / "entrypoint.sh")], check=True)
        assert os.access(LAB_ROOT / "labctl", os.X_OK)
        assert os.access(LAB_ROOT / "entrypoint.sh", os.X_OK)

    compose = yaml.safe_load((LAB_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["windows"]
    assert service["devices"] == ["/dev/kvm:/dev/kvm"]
    assert service["restart"] == "no"
    assert all(str(port).startswith("127.0.0.1:") for port in service["ports"])
    assert "${SPEC_WINDOWS_LAB_STATE_ROOT:-./state}:/state" in service["volumes"]


def test_windows_docs_state_exact_supported_tier_and_exclusions() -> None:
    docs = WINDOWS_DOC.read_text(encoding="utf-8")
    for statement in (
        "Windows 11",
        "local fixed NTFS",
        "`worktree`",
        "Codex",
        "Windows PowerShell",
        "Native Claude is unavailable",
        "Docker Desktop container",
        "UNC/network",
        "Microsoft Excel",
        "outside Spec Butler's support scope",
        "non-elevated PowerShell",
    ):
        assert statement in docs

    release_surfaces = "\n".join(
        (REPO_ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "INSTALL.md", "docs/getting-started.md")
    )
    assert "Native Windows is not supported" not in release_surfaces
    assert "docs/windows.md" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/windows.md" in (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "install_command_windows = '" in docs
    assert 'install_shell_windows = "powershell"' in docs
    assert 'argv_windows = [".venv/Scripts/python.exe", "-m", "pytest"]' in docs


def test_windows_real_provider_proof_is_separately_marked_and_one_command() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    test_source = (REPO_ROOT / "tests" / "test_windows_real_provider.py").read_text(encoding="utf-8")
    assert "windows_real_provider:" in pyproject
    assert "pytest.mark.windows_real_provider" in test_source
    assert 'str(LABCTL), "proof"' in test_source
    assert "SPEC_WINDOWS_REAL_PROVIDER" in test_source
    assert "SPEC_WINDOWS_LAB_CONFIG" in test_source
