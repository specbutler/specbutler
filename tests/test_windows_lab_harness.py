from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

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
    "job-runner.ps1",
    "lab.env.example",
    "labctl",
    "proof.ps1",
    "provision.ps1",
    "redact.py",
    "register-job.ps1",
    "runtime_proof.py",
    "toolchain.json.example",
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
    assert "provision_guest" in controller
    assert "stage_source" in controller
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
    assert "Set-EvidenceClaim" in proof
    assert "Proof must run with a non-elevated user token" in proof
    register_job = (LAB_ROOT / "register-job.ps1").read_text(encoding="utf-8")
    assert "-RunLevel Limited" in register_job
    assert "-RunLevel Highest" not in register_job
    provision = (LAB_ROOT / "provision.ps1").read_text(encoding="utf-8")
    assert 'icacls.exe $harnessRoot /grant:r "${account}:(OI)(CI)M" /T /C' in provision
    assert 'python3 "$LAB_ROOT/audit_acceptance.py"' in controller
    assert '--expected-revision "$revision"' in controller
    assert '--output "$destination/acceptance-audit.json"' in controller
    assert 'return "$audit_status"' in controller


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

    utf16_token = "sk-" + "B" * 24
    (source / "powershell.log").write_text(
        f"api_key={utf16_token}\n", encoding="utf-16"
    )
    subprocess.run(
        [sys.executable, str(LAB_ROOT / "redact.py"), str(source), str(destination)],
        check=True,
    )
    utf16_redacted = (destination / "powershell.log").read_text(encoding="utf-16")
    assert utf16_token not in utf16_redacted
    assert "[REDACTED]" in utf16_redacted
    report = json.loads((destination / "_redaction-report.json").read_text())
    assert report["status"] == "passed"
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
