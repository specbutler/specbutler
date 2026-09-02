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
    "disarm-autologon.ps1",
    "entrypoint.sh",
    "ensure-console-session.ps1",
    "exact_harness.py",
    "import_external_evidence.py",
    "job-runner.ps1",
    "job-child.ps1",
    "job-supervisor.cs",
    "job-supervisor-selftest.ps1",
    "lab.env.example",
    "labctl",
    "launch_attestation.py",
    "local_acceptance.py",
    "proof.ps1",
    "provision.ps1",
    "redact.py",
    "register-job.ps1",
    "run_with_timeout.py",
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
    assert 'local timeout="${LAB_COMPOSE_DOWN_TIMEOUT_SECONDS:-180}"' in controller
    assert 'local timeout="${LAB_DOCKER_QUERY_TIMEOUT_SECONDS:-30}"' in controller
    assert 'bounded_host_command "$host_timeout" docker compose' in controller
    assert 'bounded_host_command "$query_timeout" docker ps -a' in controller
    assert "graceful shutdown timed out; forcing the VM down" in controller
    assert "VM remained active after forced shutdown" in controller
    assert 'local keep="${LAB_PROOF_TRASH_KEEP:-1}"' in controller
    assert 'trash_keep="$(proof_trash_keep)"' in controller
    assert 'proof_trash_retention "$trash_keep" apply' in controller
    assert "provision_guest" in controller
    assert "stage_source" in controller
    assert 'mkdir -p "$STATE_ROOT/incoming"' in controller
    assert "job_submit" in controller
    assert "job_wait" in controller
    assert "ensure_console_session" in controller
    assert "guest_ssh_stdin" in controller
    assert "cleanup_job_task" in controller
    assert "preserve_unsafe_job_diagnostics" in controller
    assert "could not prove descendant cleanup" in controller
    assert "authoritative guest shutdown confirmed after unsafe job completion" in controller
    assert "collect_artifacts" in controller
    assert "verify_exact_proof_harness" in controller
    proof_controller = controller[controller.index("run_proof() {") :]
    assert proof_controller.index(
        'verify_exact_proof_harness "$revision"'
    ) < proof_controller.index("if vm_running")
    assert 'HARNESS_ROOT="$snapshot_root/tools/windows-lab"' in controller
    assert '--project-directory "$HARNESS_ROOT"' in controller
    assert "authoritative_shutdown_lab" in controller
    assert 'guest_ssh_timeout "$command_timeout"' in controller
    assert 'guest_scp_timeout "$command_timeout"' in controller
    assert 'STATE_ROOT="${SPEC_WINDOWS_LAB_STATE_ROOT:-$LAB_ROOT/state}"' in controller
    assert 'SPEC_WINDOWS_LAB_STATE_ROOT="$STATE_ROOT"' in controller
    proof = (LAB_ROOT / "proof.ps1").read_text(encoding="utf-8")
    assert "-m', 'pytest'" in proof
    assert "'repo', 'create'" in proof
    assert "'implement', '--spec', 'add-numbers'" in proof
    assert "Write-Utf8NoBom" in proof
    assert "Invoke-BoundedNativeProcess" in proof
    assert proof.count("[AllowEmptyString()]") >= 4
    assert proof.count("[AllowEmptyCollection()]") >= 3
    assert "$processHandle = $process.Handle" in proof
    assert "[int] $nativeExitCode = $process.ExitCode" in proof
    assert "taskkill.exe" in proof
    assert "timed out after $TimeoutSeconds seconds" in proof
    assert "$env:GIT_TERMINAL_PROMPT = '0'" in proof
    assert "$env:GCM_INTERACTIVE = '0'" in proof
    assert "$env:GH_PROMPT_DISABLED = '1'" in proof
    assert "'auth', 'setup-git', '--hostname', 'github.com'" in proof
    assert "'config', '--global', 'credential.interactive', 'false'" in proof
    assert "'repo', 'create', $repositorySlug, '--private'" in proof
    assert "--source', $fixtureRoot" not in proof
    assert '$expectedOrigin = "https://github.com/$repositorySlug.git"' in proof
    assert "'remote', 'get-url', 'origin'" in proof
    assert "'push', '-u', 'origin', 'main'" in proof
    assert "'push', 'origin', 'main'" in proof
    assert "-TimeoutSeconds 180" in proof
    assert "unattended_git_auth = $true" in proof
    assert "raw_https_push = 'passed'" in proof
    assert "native_command_timeouts = $true" in proof
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
    utf8_writer = proof[
        proof.index("function Write-Utf8NoBom") : proof.index("function Wait-Condition")
    ]
    assert "[AllowEmptyString()]" in utf8_writer
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
    assert register_job.index("$desktopSessions") < register_job.index("Register-ScheduledTask")
    assert register_job.index("Start-ScheduledTask") < register_job.index("$receipt")
    assert "launch_nonce" in register_job
    assert "Unregister-ScheduledTask" in register_job
    runner = (LAB_ROOT / "job-runner.ps1").read_text(encoding="utf-8")
    assert "LaunchNonce" in runner
    assert "started.json.tmp" in runner
    assert runner.index("Move-Item -LiteralPath $startedTemp") < runner.index(
        "[SpecButlerLabJobSupervisor]::Run"
    )
    assert runner.index("Move-Item -LiteralPath $startedTemp") < runner.index("$releaseDeadline")
    assert runner.index("$releasedNonce -cne $LaunchNonce") < runner.index(
        "[SpecButlerLabJobSupervisor]::Run"
    )
    assert "job_supervision = 'kill-on-close-job'" in runner
    assert "descendants_gone = $false" in runner
    assert "$result.descendants_gone = [bool]$supervised.DescendantsGone" in runner
    assert "[System.IO.File]::AppendAllText" in runner
    child_runner = (LAB_ROOT / "job-child.ps1").read_text(encoding="utf-8")
    assert "[Console]::OutputEncoding = $utf8" in child_runner
    assert "$global:OutputEncoding = $utf8" in child_runner
    supervisor = (LAB_ROOT / "job-supervisor.cs").read_text(encoding="utf-8")
    for boundary in (
        "CREATE_SUSPENDED",
        "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
        "CreateJobObject",
        "SetInformationJobObject",
        "AssignProcessToJobObject",
        "ResumeThread",
        "TerminateJobObject",
        "TerminateProcess",
        "QueryInformationJobObject",
    ):
        assert boundary in supervisor
    assert (
        supervisor.index("CreateProcess(")
        < supervisor.index("AssignProcessToJobObject(job, processInformation.hProcess)")
        < supervisor.index("ResumeThread(processInformation.hThread)")
    )
    assert supervisor.index("!assigned") < supervisor.index(
        "TerminateProcess(processInformation.hProcess, 125)"
    )
    supervisor_selftest = (LAB_ROOT / "job-supervisor-selftest.ps1").read_text(
        encoding="utf-8"
    )
    for probe in (
        "argv_fidelity",
        "exit_propagation",
        "timeout_tree_cleanup",
        "lingering_tree_cleanup",
        "utf8_stdout_stderr",
        "redaction",
    ):
        assert probe in supervisor_selftest
    assert "$PSVersionTable.PSEdition -eq 'Desktop'" in supervisor_selftest
    assert "$PSVersionTable.PSVersion.Major -eq 5" in supervisor_selftest
    console_session = (LAB_ROOT / "ensure-console-session.ps1").read_text(encoding="utf-8")
    assert "[Console]::In.ReadToEnd()" in console_session
    assert "DefaultPassword" in console_session
    assert "Remove-ItemProperty" in console_session
    assert "AutoLogonCount" in console_session
    arm_session = console_session[console_session.index("if ($Mode -eq 'Arm')") :]
    assert (
        arm_session.index("Register-ScheduledTask")
        < arm_session.index(
            "New-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon"
        )
        < arm_session.index(
            "New-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount"
        )
        < arm_session.index(
            "New-ItemProperty -LiteralPath $winlogon -Name DefaultPassword"
        )
    )
    assert "-Value 1 -PropertyType DWord" in console_session
    assert "disarm-autologon.ps1" in console_session
    assert "takeown.exe '/F' $cleanupScript '/A'" in console_session
    assert "$systemSidValue = 'S-1-5-18'" in console_session
    assert "$administratorsSidValue = 'S-1-5-32-544'" in console_session
    assert "$usersSidValue = 'S-1-5-32-545'" in console_session
    assert "[switch] $IncludeUsersReadAndExecute" in console_session
    assert "FileSystemRights]::ReadAndExecute -bor" in console_session
    assert "$acl.SetAccessRuleProtection($true, $false)" in console_session
    assert "$rules.Count -ne $expectedRules.Count" in console_session
    assert "FileAttributes]::ReparsePoint" in console_session
    assert "fsutil.exe hardlink list $LiteralPath" in console_session
    assert "Expected a single-link file" in console_session
    assert "Assert-TrustedExistingPath" in console_session
    assert "grants write access to an untrusted principal" in console_session
    assert "FileSystemRights]::DeleteSubdirectoriesAndFiles" in console_session
    assert "'/T' '/C'" not in console_session
    assert "if (-not (Test-Path -LiteralPath $winlogon))" in console_session
    assert "New-Item -Path $winlogon -Force" not in console_session
    assert "Invoke-DirectDisarm" in console_session
    assert "& $cleanupSource -RemoveTask" not in console_session
    assert "& $cleanupScript -RemoveTask" not in console_session
    assert (
        console_session.index(
            "Set-ExactProtectedAcl -LiteralPath $harnessRoot -Container $true `"
        )
        < console_session.index("[System.IO.File]::ReadAllBytes($cleanupSource)")
        < console_session.index(
            "Set-ExactProtectedAcl -LiteralPath $secureRoot -Container $true"
        )
        < console_session.index(
            "[System.IO.FileMode]::CreateNew"
        )
        < console_session.index(
            "Set-ExactProtectedAcl -LiteralPath $cleanupScript -Container $false"
        )
        < console_session.index("Register-ScheduledTask")
    )
    assert (
        "-IncludeIdentity -IncludeUsersReadAndExecute" in console_session
    )
    assert (
        "Set-ExactProtectedAcl -LiteralPath $secureRoot -Container $true\n"
        in console_session
    )
    assert "Remove-Item -LiteralPath $cleanupScript -Force -ErrorAction Stop" in console_session
    assert "Console cleanup script remained after checked removal" in console_session
    assert (
        console_session.index(
            "Set-ExactProtectedAcl -LiteralPath $secureRoot -Container $true"
        )
        < console_session.index(
            "Get-FileHash -LiteralPath $cleanupScript -Algorithm SHA256"
        )
        < console_session.index("Register-ScheduledTask")
    )
    assert controller.index("ensure_console_session()") < controller.index("job_submit()")
    assert '"$STATE_ROOT/secrets/admin-password"' in controller
    assert "-Mode Arm' >/dev/null" in controller
    assert "-Mode Disarm' >/dev/null" in controller
    assert "interactive job $name returned to Ready" in controller
    assert "capture_launch_attestation" in controller
    launch_attestation = (LAB_ROOT / "launch_attestation.py").read_text(encoding="utf-8")
    assert "captured-before-release" in launch_attestation
    assert "receipt_sha256" in launch_attestation
    assert controller.index('capture_launch_attestation "$name" "$nonce"') < controller.index("$name.release")
    release_command = controller[controller.index("$name.release") :]
    assert release_command.index("WriteAllText") < release_command.index("Move-Item")
    arm_call = controller[controller.index("guest_ssh_stdin") : controller.index("Restart-Computer")]
    assert "if ! guest_ssh_stdin" in arm_call
    assert "shutdown_lab" in arm_call
    recovery = controller[
        controller.index("ensure_console_session() {") : controller.index("job_submit() {")
    ]
    assert recovery.rindex("require_admin_password_state") < recovery.index(
        "guest_ssh_stdin"
    )
    assert 'guest_scp "specadmin@127.0.0.1:C:/SpecHarness/jobs/$name.started.json"' in controller
    provision = (LAB_ROOT / "provision.ps1").read_text(encoding="utf-8")
    assert 'icacls.exe $harnessRoot /grant:r "${account}:(OI)(CI)M" /T /C' in provision
    assert "-Filter 'codex-code-mode-host-*.exe'" in provision
    assert "$codexHostAlias = Join-Path $codexRoot 'codex-code-mode-host.exe'" in provision
    assert "$codexHostSourceHash -ne $codexHostAliasHash" in provision
    assert "Codex code-mode host is missing from its required canonical path" in proof
    assert '$env:TEMP = $temp' in runner
    assert '$env:TMP = $temp' in runner
    assert 'python3 "$HARNESS_ROOT/audit_acceptance.py"' in controller
    assert '--manifest-source-path "tools/windows-lab/acceptance-manifest.json"' in controller
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
    assert "LAB_COMPOSE_DOWN_TIMEOUT_SECONDS=180" in env_example
    assert "LAB_DOCKER_QUERY_TIMEOUT_SECONDS=30" in env_example
    assert "LAB_INTERACTIVE_SESSION_GRACE_SECONDS=30" in env_example
    assert "LAB_INTERACTIVE_SESSION_REPAIR=1" in env_example
    assert "LAB_PROOF_TRASH_KEEP=1" in env_example
    proof_run = controller[
        controller.index("run_proof() {") : controller.index('command="${1:-help}"')
    ]
    assert proof_run.index("require_admin_password_state") < proof_run.index(
        "shutdown_lab"
    )
    assert (
        proof_run.index("shutdown_lab")
        < proof_run.index('proof_trash_retention "$trash_keep"')
        < proof_run.index('reset_lab "$LAB_BASELINE"')
        < proof_run.index('proof_trash_retention "$trash_keep" apply')
        < proof_run.index('require_free_space "$STATE_ROOT"')
        < proof_run.index("up_lab")
    )
    manifest = json.loads(
        (LAB_ROOT / "acceptance-manifest.json").read_text(encoding="utf-8")
    )
    release_five = next(
        item
        for item in manifest["criteria"]
        if item["id"] == "windows-ci-e2e-release.5"
    )
    assert any(
        check["id"] == "release.5.interactive-launch"
        for check in release_five["checks"]
    )
    assert any(
        check["id"] == "release.5.interactive-descendants"
        for check in release_five["checks"]
    )
    assert any(
        check["id"] == "release.5.exact-harness"
        for check in release_five["checks"]
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
    assert "refusing to generate a password that would not match the guest" in controller
    assert "stat -c '%u:%a:%s'" in controller
    assert "stat -f '%u:%Lp:%z'" in controller
    for guest_state in (
        '"$STATE_ROOT/unattend.iso"',
        '"$STATE_ROOT/disk/run.qcow2"',
        '"$STATE_ROOT/run/nvram.fd"',
        '"$STATE_ROOT/run/tpm"',
        '"$STATE_ROOT/baselines" "$STATE_ROOT/trash"',
    ):
        assert guest_state in controller


@pytest.mark.parametrize(
    "credential_state",
    ["missing", "bad-mode", "symlink", "empty-effective", "malformed"],
)
def test_windows_lab_proof_validates_private_credential_before_mutation(
    tmp_path: Path, credential_state: str
) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    (state / "disk").mkdir(parents=True)
    overlay = state / "disk" / "run.qcow2"
    overlay.write_bytes(b"do-not-reset")
    if credential_state == "bad-mode":
        secret_dir = state / "secrets"
        secret_dir.mkdir()
        password = secret_dir / "admin-password"
        password.write_text("Sb1!" + ("a" * 32) + "\n", encoding="ascii")
        password.chmod(0o644)
    elif credential_state == "symlink":
        secret_dir = state / "secrets"
        secret_dir.mkdir()
        password = secret_dir / "admin-password"
        password.symlink_to(tmp_path / "outside-secret")
    elif credential_state in {"empty-effective", "malformed"}:
        secret_dir = state / "secrets"
        secret_dir.mkdir()
        password = secret_dir / "admin-password"
        value = "\n" if credential_state == "empty-effective" else "bad!" + ("a" * 32) + "\n"
        password.write_text(value, encoding="ascii")
        password.chmod(0o600)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/not-used.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n"
        "LAB_BASELINE=toolchain\n"
        "LAB_GITHUB_OWNER=example\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "proof"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
            "SPEC_WINDOWS_ACCEPTANCE_EVIDENCE_ROOT": str(evidence),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "private lab credential" in result.stderr
    assert overlay.read_bytes() == b"do-not-reset"


def test_windows_lab_credential_preflight_falls_back_to_bsd_stat(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    secret_dir = state / "secrets"
    disk_dir = state / "disk"
    fake_bin = tmp_path / "bin"
    secret_dir.mkdir(parents=True)
    disk_dir.mkdir()
    fake_bin.mkdir()
    password = secret_dir / "admin-password"
    password.write_text("bad!" + ("a" * 32) + "\n", encoding="ascii")
    password.chmod(0o600)
    overlay = disk_dir / "run.qcow2"
    overlay.write_bytes(b"do-not-reset")
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "if sys.argv[1] == '-c':\n"
        "    raise SystemExit(1)\n"
        "if sys.argv[1:3] != ['-f', '%u:%Lp:%z']:\n"
        "    raise SystemExit(2)\n"
        "item = os.stat(sys.argv[-1])\n"
        "print(f'{item.st_uid}:{item.st_mode & 0o777:o}:{item.st_size}')\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/not-used.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n"
        "LAB_BASELINE=toolchain\n"
        "LAB_GITHUB_OWNER=example\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "proof"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))),
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
            "SPEC_WINDOWS_ACCEPTANCE_EVIDENCE_ROOT": str(evidence),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "does not use the generated format" in result.stderr
    assert overlay.read_bytes() == b"do-not-reset"


@pytest.mark.parametrize(
    "guest_state",
    [
        "disk/run.qcow2",
        "run/nvram.fd",
        "run/tpm/state.bin",
        "unattend.iso",
        "trash/retained/run.qcow2",
    ],
)
def test_windows_lab_init_refuses_missing_credential_before_identity_mutation(
    tmp_path: Path, guest_state: str
) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    path = state / guest_state
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing-guest-state")
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/not-used.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "init"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "refusing to generate a password" in result.stderr
    assert not (state / "identity.env").exists()
    assert not (state / "secrets" / "admin-password").exists()
    assert path.read_bytes() == b"existing-guest-state"


@pytest.mark.parametrize(
    ("credential", "mode"),
    [
        ("\n", 0o600),
        ("bad!" + ("a" * 32) + "\n", 0o600),
        ("Sb1!" + ("a" * 32) + "\n", 0o644),
    ],
)
def test_windows_lab_init_refuses_invalid_credential_before_identity_mutation(
    tmp_path: Path, credential: str, mode: int
) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    secret_dir = state / "secrets"
    secret_dir.mkdir(parents=True)
    password = secret_dir / "admin-password"
    password.write_text(credential, encoding="ascii")
    password.chmod(mode)
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/not-used.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "init"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "private lab credential" in result.stderr
    assert not (state / "identity.env").exists()
    assert password.read_text(encoding="ascii") == credential


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
    assert report["undecodable_text_files"] == []


def test_windows_lab_redactor_fails_closed_for_undecodable_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "raw"
    destination = tmp_path / "sanitized"
    source.mkdir()
    secret = b"ghp_" + (b"A" * 36)
    (source / "job.log").write_bytes(b"\xff\x00legacy\x80" + secret)

    result = subprocess.run(
        [sys.executable, str(LAB_ROOT / "redact.py"), str(source), str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert secret not in (destination / "job.log").read_bytes()
    report = json.loads((destination / "_redaction-report.json").read_text())
    assert report["status"] == "failed"
    assert report["undecodable_text_files"] == ["job.log"]


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
    assert "[SpecButlerLabJobSupervisor]::Run" in runner
    assert "$exitCode = [int]$supervised.ExitCode" in runner


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


@pytest.mark.parametrize(
    ("task_state", "cleanup_fails"),
    [("Ready", False), ("Missing", False), ("Ready", True)],
)
def test_windows_lab_job_wait_fails_fast_and_unregisters_stale_task(
    tmp_path: Path, task_state: str, cleanup_fails: bool
) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    state.mkdir()
    fake_bin.mkdir()
    (state / "identity.env").write_text("LAB_UID=1\n", encoding="utf-8")
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/fake.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n",
        encoding="utf-8",
    )
    cleanup_log = tmp_path / "cleanup.log"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "command = sys.argv[-1]\n"
        "if 'Test-Path C:\\\\SpecHarness\\\\jobs\\\\demo.done.json' in command:\n"
        "    print('False')\n"
        "elif 'Stop-ScheduledTask' in command:\n"
        "    Path(os.environ['FAKE_CLEANUP_LOG']).write_text(command, encoding='utf-8')\n"
        "    if os.environ['FAKE_CLEANUP_FAIL'] == '1':\n"
        "        raise SystemExit(23)\n"
        "elif 'Get-ScheduledTask' in command:\n"
        "    print(os.environ['FAKE_TASK_STATE'])\n"
        "else:\n"
        "    raise SystemExit(f'unexpected fake SSH command: {command}')\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:3] == ['ps', '-a']:\n"
        "    print('specbutler-windows-lab|exited')\n"
        "else:\n"
        "    raise SystemExit(f'unexpected fake Docker command: {sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "job", "wait", "demo", "10"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
            "FAKE_TASK_STATE": task_state,
            "FAKE_CLEANUP_LOG": str(cleanup_log),
            "FAKE_CLEANUP_FAIL": "1" if cleanup_fails else "0",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    expected = (
        "returned to Ready without a completion record"
        if task_state == "Ready"
        else "entered unexpected task state: Missing"
    )
    assert expected in result.stderr
    cleanup = cleanup_log.read_text(encoding="utf-8")
    assert "Stop-ScheduledTask" in cleanup
    assert "Unregister-ScheduledTask" in cleanup
    if cleanup_fails:
        assert "could not verify scheduled-task cleanup" in result.stderr
        assert "authoritative guest shutdown confirmed" in result.stderr


@pytest.mark.parametrize(
    "compose_down_result",
    ["success", "failure", "hang", "query-hang-after-down"],
)
def test_windows_lab_unsafe_job_forces_and_verifies_guest_shutdown(
    tmp_path: Path,
    compose_down_result: str,
) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    state.mkdir()
    fake_bin.mkdir()
    (state / "identity.env").write_text("LAB_UID=1\n", encoding="utf-8")
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/fake.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n"
        "LAB_GUEST_COMMAND_TIMEOUT_SECONDS=1\n"
        "LAB_SHUTDOWN_TIMEOUT_SECONDS=1\n"
        "LAB_COMPOSE_DOWN_TIMEOUT_SECONDS=1\n"
        "LAB_DOCKER_QUERY_TIMEOUT_SECONDS=1\n",
        encoding="utf-8",
    )
    ssh_log = tmp_path / "ssh.log"
    docker_log = tmp_path / "docker.log"
    docker_state = tmp_path / "docker.state"
    docker_state.write_text("running", encoding="ascii")
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "command = sys.argv[-1]\n"
        "with Path(os.environ['FAKE_SSH_LOG']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(command + '\\n')\n"
        "if 'Test-Path C:\\\\SpecHarness\\\\jobs\\\\demo.done.json' in command:\n"
        "    print('True')\n"
        "elif 'Get-Content C:\\\\SpecHarness\\\\jobs\\\\demo.done.json' in command:\n"
        "    print(json.dumps({'status': 'failed', 'descendants_gone': False}))\n"
        "elif 'Compress-Archive' in command or 'Stop-Computer' in command:\n"
        "    time.sleep(30)\n"
        "else:\n"
        "    raise SystemExit(f'unexpected fake SSH command: {command}')\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_scp = fake_bin / "scp"
    fake_scp.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n", encoding="utf-8")
    fake_scp.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_DOCKER_STATE'])\n"
        "with Path(os.environ['FAKE_DOCKER_LOG']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['ps', '-a']:\n"
        "    current = state.read_text(encoding='ascii')\n"
        "    if current == 'query-hang':\n"
        "        time.sleep(30)\n"
        "    if current == 'running':\n"
        "        print('specbutler-windows-lab|running')\n"
        "elif sys.argv[1] == 'compose' and 'down' in sys.argv[2:]:\n"
        "    if os.environ['FAKE_COMPOSE_DOWN_RESULT'] == 'hang':\n"
        "        time.sleep(30)\n"
        "    if os.environ['FAKE_COMPOSE_DOWN_RESULT'] == 'failure':\n"
        "        raise SystemExit(23)\n"
        "    next_state = (\n"
        "        'query-hang'\n"
        "        if os.environ['FAKE_COMPOSE_DOWN_RESULT'] == 'query-hang-after-down'\n"
        "        else 'absent'\n"
        "    )\n"
        "    state.write_text(next_state, encoding='ascii')\n"
        "else:\n"
        "    raise SystemExit(f'unexpected fake Docker command: {sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "job", "wait", "demo", "10"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_DOCKER_STATE": str(docker_state),
            "FAKE_COMPOSE_DOWN_RESULT": compose_down_result,
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert result.returncode == 1
    assert "could not prove descendant cleanup" in result.stderr
    assert "down --timeout 30" in docker_log.read_text(encoding="utf-8")
    assert "Stop-ScheduledTask" not in ssh_log.read_text(encoding="utf-8")
    marker = state / "unsafe-cleanup" / "demo" / "authoritative-shutdown.txt"
    if compose_down_result != "success":
        assert not marker.exists()
        assert "could not confirm authoritative guest shutdown" in result.stderr
        assert "authoritative guest shutdown confirmed" not in result.stderr
    else:
        assert marker.read_text(encoding="ascii").strip() == (
            "authoritative_guest_shutdown=confirmed"
        )
        assert "authoritative guest shutdown confirmed" in result.stderr


@pytest.mark.parametrize("failure_mode", ["credential", "arm", "register"])
def test_windows_lab_submit_failure_cleans_up_without_exposing_password(
    tmp_path: Path, failure_mode: str
) -> None:
    if os.name == "nt":
        pytest.skip("the host controller is a Bash program")
    bash = shutil.which("bash")
    assert bash
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    (state / "secrets").mkdir(parents=True)
    fake_bin.mkdir()
    (state / "identity.env").write_text("LAB_UID=1\n", encoding="utf-8")
    password = "Sb1!" + ("d" * 32)
    (state / "secrets" / "admin-password").write_text(
        password + "\n", encoding="ascii"
    )
    (state / "secrets" / "admin-password").chmod(
        0o644 if failure_mode == "credential" else 0o600
    )
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/fake.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n"
        "LAB_INTERACTIVE_SESSION_GRACE_SECONDS=0\n"
        "LAB_INTERACTIVE_SESSION_REPAIR=1\n",
        encoding="utf-8",
    )
    ssh_log = tmp_path / "ssh.log"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "command = sys.argv[-1]\n"
        "with Path(os.environ['FAKE_SSH_LOG']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(command + '\\n')\n"
        "mode = os.environ['FAKE_FAILURE_MODE']\n"
        "if '-Mode Check' in command:\n"
        "    raise SystemExit(2 if mode in {'credential', 'arm'} else 0)\n"
        "if '-Mode Arm' in command:\n"
        "    sys.stdin.read()\n"
        "    raise SystemExit(23 if mode == 'arm' else 0)\n"
        "if 'register-job.ps1' in command:\n"
        "    raise SystemExit(23 if mode == 'register' else 0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    for command in ("scp", "docker"):
        executable = fake_bin / command
        if command == "scp":
            body = "raise SystemExit(0)"
        else:
            body = (
                "print('specbutler-windows-lab|exited') "
                "if sys.argv[1:3] == ['ps', '-a'] else (_ for _ in ()).throw(SystemExit(2))"
            )
        executable.write_text(
            f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8"
        )
        executable.chmod(0o755)
    job_script = tmp_path / "job.ps1"
    job_script.write_text("exit 0\n", encoding="ascii")
    result = subprocess.run(
        [bash, str(LAB_ROOT / "labctl"), "job", "submit", "demo", str(job_script)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SPEC_WINDOWS_LAB_STATE_ROOT": str(state),
            "SPEC_WINDOWS_LAB_CONFIG": str(config),
            "SPEC_WINDOWS_TOOLCHAIN_CONFIG": str(tmp_path / "toolchain.json"),
            "FAKE_FAILURE_MODE": failure_mode,
            "FAKE_SSH_LOG": str(ssh_log),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    commands = ssh_log.read_text(encoding="utf-8")
    assert password not in commands
    assert password not in result.stdout
    assert password not in result.stderr
    if failure_mode == "arm":
        assert "-Mode Arm" in commands
        assert "-Mode Disarm" in commands
        assert "authoritative guest shutdown was confirmed" in result.stderr
    elif failure_mode == "credential":
        assert "-Mode Arm" not in commands
        assert "private lab credential" in result.stderr
        assert "authoritative guest shutdown" not in result.stderr
    else:
        assert "register-job.ps1" in commands
        assert "Stop-ScheduledTask" in commands
        assert "registration or launch acknowledgement failed" in result.stderr


def _load_launch_attestation_module():
    path = LAB_ROOT / "launch_attestation.py"
    spec = importlib.util.spec_from_file_location("windows_launch_attestation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_exact_harness_module():
    path = LAB_ROOT / "exact_harness.py"
    spec = importlib.util.spec_from_file_location("windows_exact_harness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_host_timeout_module():
    path = LAB_ROOT / "run_with_timeout.py"
    spec = importlib.util.spec_from_file_location("windows_host_timeout", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_lab_portable_timeout_terminates_the_command_group(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("the host timeout helper is for POSIX lab controllers")
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        "import os\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "def stopped(_signum, _frame):\n"
        "    (root / 'child-stopped').write_text('yes', encoding='ascii')\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "(root / 'child-ready').write_text('yes', encoding='ascii')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "def stopped(_signum, _frame):\n"
        "    (root / 'parent-stopped').write_text('yes', encoding='ascii')\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "subprocess.Popen([sys.executable, str(root / 'child.py'), str(root)])\n"
        "deadline = time.monotonic() + 5\n"
        "while not (root / 'child-ready').exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise SystemExit('child did not become ready')\n"
        "    time.sleep(0.01)\n"
        "(root / 'parent-ready').write_text('yes', encoding='ascii')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(LAB_ROOT / "run_with_timeout.py"),
            "1",
            "--",
            sys.executable,
            str(parent),
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 124
    assert (tmp_path / "parent-ready").is_file()
    assert (tmp_path / "child-ready").is_file()
    assert (tmp_path / "parent-stopped").read_text(encoding="ascii") == "yes"
    assert (tmp_path / "child-stopped").read_text(encoding="ascii") == "yes"


def test_windows_lab_portable_timeout_escalates_for_a_lingering_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("the host timeout helper is for POSIX lab controllers")
    child = tmp_path / "ignoring-child.py"
    parent = tmp_path / "short-parent.py"
    child.write_text(
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "def observed(_signum, _frame):\n"
        "    (root / 'child-saw-term').write_text('yes', encoding='ascii')\n"
        "signal.signal(signal.SIGTERM, observed)\n"
        "(root / 'child-ready').write_text('yes', encoding='ascii')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "def stopped(_signum, _frame):\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "subprocess.Popen([sys.executable, str(root / 'ignoring-child.py'), str(root)])\n"
        "while not (root / 'child-ready').exists():\n"
        "    time.sleep(0.01)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    module = _load_host_timeout_module()
    monkeypatch.setattr(module, "KILL_GRACE_SECONDS", 0.2)
    assert module.run(1, [sys.executable, str(parent), str(tmp_path)]) == 124
    assert (tmp_path / "child-saw-term").read_text(encoding="ascii") == "yes"


def test_windows_lab_exact_harness_rejects_dirty_or_different_controller(
    tmp_path: Path,
) -> None:
    module = _load_exact_harness_module()
    repo = tmp_path / "repo"
    harness = repo / "tools" / "windows-lab"
    harness.mkdir(parents=True)
    (repo / ".gitattributes").write_text(
        "tools/windows-lab/* text eol=lf\n",
        encoding="ascii",
    )
    labctl = harness / "labctl"
    labctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    labctl.chmod(0o755)
    verifier = harness / "exact_harness.py"
    verifier.write_bytes((LAB_ROOT / "exact_harness.py").read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Spec Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "spec-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Model a Windows checkout on Linux: the worktree is fully CRLF-smudged,
    # while the exact committed blob remains LF.
    labctl.write_bytes(
        subprocess.check_output(
            ["git", "--no-replace-objects", "show", f"{revision}:tools/windows-lab/labctl"],
            cwd=repo,
        ).replace(b"\n", b"\r\n")
    )
    verifier.write_bytes(
        subprocess.check_output(
            [
                "git",
                "--no-replace-objects",
                "show",
                f"{revision}:tools/windows-lab/exact_harness.py",
            ],
            cwd=repo,
        ).replace(b"\n", b"\r\n")
    )
    assert b"\r\n" in labctl.read_bytes()

    output = tmp_path / "exact.json"
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result = module.verify_exact_harness(repo, revision, output, snapshot)
    assert result["status"] == "passed"
    assert result["source_revision"] == revision
    assert result["snapshot_materialized"] is True
    assert result["snapshot_files_verified"] == result["files_verified"]
    assert set(result["sha256"]) == {
        "tools/windows-lab/exact_harness.py",
        "tools/windows-lab/labctl",
    }
    snapshot_labctl = snapshot / "tools" / "windows-lab" / "labctl"
    committed_labctl = snapshot_labctl.read_bytes()
    assert committed_labctl == subprocess.check_output(
        ["git", "show", f"{revision}:tools/windows-lab/labctl"],
        cwd=repo,
    )
    assert b"\r\n" not in committed_labctl
    assert os.access(snapshot_labctl, os.X_OK)

    # Local attributes and filter commands are mutable and therefore cannot
    # authenticate the controller that is about to drive native evidence.
    (repo / ".gitattributes").write_text(
        "tools/windows-lab/* text eol=lf\n"
        "tools/windows-lab/labctl filter=mask\n",
        encoding="ascii",
    )
    subprocess.run(
        [
            "git",
            "config",
            "filter.mask.clean",
            "git show HEAD:tools/windows-lab/labctl",
        ],
        cwd=repo,
        check=True,
    )
    labctl.write_text("#!/usr/bin/env bash\nexit 23\n", encoding="utf-8")
    assert snapshot_labctl.read_bytes() == committed_labctl
    dirty_snapshot = tmp_path / "dirty-snapshot"
    dirty_snapshot.mkdir()
    with pytest.raises(module.HarnessMismatch, match="differs"):
        module.verify_exact_harness(
            repo,
            revision,
            tmp_path / "dirty.json",
            dirty_snapshot,
        )
    (repo / ".gitattributes").write_text(
        "tools/windows-lab/* text eol=lf\n",
        encoding="ascii",
    )
    subprocess.run(
        ["git", "config", "--unset", "filter.mask.clean"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "tools/windows-lab/labctl"], cwd=repo, check=True)
    staged_snapshot = tmp_path / "staged-snapshot"
    staged_snapshot.mkdir()
    with pytest.raises(module.HarnessMismatch, match="differs"):
        module.verify_exact_harness(
            repo,
            revision,
            tmp_path / "staged.json",
            staged_snapshot,
        )
    labctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "tools/windows-lab/labctl"], cwd=repo, check=True)
    (harness / "untracked.ps1").write_text("exit 0\n", encoding="utf-8")
    untracked_snapshot = tmp_path / "untracked-snapshot"
    untracked_snapshot.mkdir()
    with pytest.raises(module.HarnessMismatch, match="commit or remove"):
        module.verify_exact_harness(
            repo,
            revision,
            tmp_path / "untracked.json",
            untracked_snapshot,
        )

    nonempty = tmp_path / "nonempty-snapshot"
    nonempty.mkdir()
    (nonempty / "stale").write_text("stale", encoding="ascii")
    (harness / "untracked.ps1").unlink()
    with pytest.raises(module.HarnessMismatch, match="must be empty"):
        module.verify_exact_harness(
            repo,
            revision,
            tmp_path / "nonempty.json",
            nonempty,
        )


def test_windows_lab_exact_harness_ignores_git_replacement_objects(tmp_path: Path) -> None:
    module = _load_exact_harness_module()
    repo = tmp_path / "repo"
    harness = repo / "tools" / "windows-lab"
    harness.mkdir(parents=True)
    labctl = harness / "labctl"
    labctl.write_text("#!/usr/bin/env bash\necho GOOD\n", encoding="utf-8")
    labctl.chmod(0o755)
    (harness / "exact_harness.py").write_bytes((LAB_ROOT / "exact_harness.py").read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Spec Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "spec-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "good"], cwd=repo, check=True, capture_output=True)
    good_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="ascii"
    ).strip()
    labctl.write_text("#!/usr/bin/env bash\necho EVIL\n", encoding="utf-8")
    subprocess.run(["git", "add", "tools/windows-lab/labctl"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=repo, check=True, capture_output=True)
    evil_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="ascii"
    ).strip()
    subprocess.run(
        ["git", "checkout", "--detach", good_revision],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "replace", good_revision, evil_revision], cwd=repo, check=True)
    labctl.write_bytes(
        subprocess.check_output(
            ["git", "--no-replace-objects", "show", f"{evil_revision}:tools/windows-lab/labctl"],
            cwd=repo,
        )
    )

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with pytest.raises(module.HarnessMismatch, match="differs"):
        module.verify_exact_harness(
            repo,
            good_revision,
            tmp_path / "exact.json",
            snapshot,
        )


def _write_launch_receipt(path: Path, *, nonce: str) -> dict[str, object]:
    receipt: dict[str, object] = {
        "job": "proof-test",
        "launch_nonce": nonce,
        "user_name": "LAB\\specadmin",
        "user_sid": "S-1-5-21-1-2-3-1001",
        "session_id": 1,
        "started_at": "2026-09-01T00:00:00Z",
    }
    path.write_text(json.dumps(receipt), encoding="ascii")
    return receipt


def test_windows_lab_launch_attestation_binds_exact_nonce_before_release(
    tmp_path: Path,
) -> None:
    module = _load_launch_attestation_module()
    nonce = "ab" * 16
    receipt_path = tmp_path / "started.json"
    receipt = _write_launch_receipt(receipt_path, nonce=nonce)
    output = tmp_path / "attestation.json"
    payload = module.capture_attestation(
        receipt_path,
        output,
        expected_job="proof-test",
        expected_nonce=nonce,
    )
    assert payload["status"] == "captured-before-release"
    assert payload["expected_nonce"] == nonce
    assert payload["receipt"] == receipt
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    context = tmp_path / "user-context.json"
    context.write_text(
        json.dumps(
            {
                "user_name": receipt["user_name"],
                "user_sid": receipt["user_sid"],
                "session_id": receipt["session_id"],
            }
        ),
        encoding="ascii",
    )
    assert (
        module.validate_attestation(
            output, receipt_path, context, expected_job="proof-test"
        )["expected_nonce"]
        == nonce
    )


def test_windows_lab_launch_attestation_rejects_wrong_or_mutated_receipt(
    tmp_path: Path,
) -> None:
    module = _load_launch_attestation_module()
    nonce = "cd" * 16
    receipt_path = tmp_path / "started.json"
    receipt = _write_launch_receipt(receipt_path, nonce=nonce)
    with pytest.raises(module.AttestationError, match="exact host nonce"):
        module.capture_attestation(
            receipt_path,
            tmp_path / "wrong.json",
            expected_job="proof-test",
            expected_nonce="ef" * 16,
        )
    output = tmp_path / "attestation.json"
    module.capture_attestation(
        receipt_path,
        output,
        expected_job="proof-test",
        expected_nonce=nonce,
    )
    context = tmp_path / "user-context.json"
    context.write_text(json.dumps(receipt), encoding="ascii")
    receipt["session_id"] = 2
    receipt_path.write_text(json.dumps(receipt), encoding="ascii")
    with pytest.raises(module.AttestationError, match="host and guest"):
        module.validate_attestation(
            output, receipt_path, context, expected_job="proof-test"
        )


def test_windows_lab_launch_attestation_rejects_wrong_proof_context(
    tmp_path: Path,
) -> None:
    module = _load_launch_attestation_module()
    nonce = "12" * 16
    receipt_path = tmp_path / "started.json"
    receipt = _write_launch_receipt(receipt_path, nonce=nonce)
    output = tmp_path / "attestation.json"
    module.capture_attestation(
        receipt_path,
        output,
        expected_job="proof-test",
        expected_nonce=nonce,
    )
    receipt["user_sid"] = "S-1-5-21-wrong"
    context = tmp_path / "user-context.json"
    context.write_text(json.dumps(receipt), encoding="ascii")
    with pytest.raises(module.AttestationError, match="proof context"):
        module.validate_attestation(
            output, receipt_path, context, expected_job="proof-test"
        )


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
