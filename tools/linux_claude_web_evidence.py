#!/usr/bin/env python3
"""Run the credentialed Linux Claude web proof and retain exact-revision evidence.

The real-provider pytest owns the proof observations and writes a private,
single-run receipt only after its HTTP/SSE, retained-context, and process-reap
assertions pass.  This runner validates that receipt before atomically
publishing the acceptance artifact.  A failed or skipped test therefore cannot
leave a passing result behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_NODE = (
    "tests/test_linux_claude_real_provider.py::"
    "test_linux_real_claude_web_chat_preserves_context_and_reaps_provider"
)
REAL_PROVIDER_ENV = "SPEC_LINUX_CLAUDE_REAL_PROVIDER"
RECEIPT_PATH_ENV = "SPEC_LINUX_CLAUDE_PROOF_RECEIPT"
EXPECTED_REVISION_ENV = "SPEC_LINUX_CLAUDE_EXPECTED_REVISION"
CHALLENGE_ENV = "SPEC_LINUX_CLAUDE_PROOF_CHALLENGE"
REVISION_RE = re.compile(r"[0-9a-f]{40}")


class EvidenceError(RuntimeError):
    """The proof did not establish a publishable result."""


def _git(source_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=source_root,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise EvidenceError(f"git {args[0]} failed with exit code {completed.returncode}")
    return completed.stdout.strip()


def _checkout_revision(source_root: Path) -> str:
    revision = _git(source_root, "rev-parse", "HEAD").lower()
    if not REVISION_RE.fullmatch(revision):
        raise EvidenceError("tested checkout did not resolve to an exact 40-character commit")
    return revision


def _require_clean_checkout(source_root: Path) -> None:
    status = _git(source_root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise EvidenceError("tested checkout has tracked or untracked changes")


def _require_safe_output(output: Path) -> None:
    try:
        output.relative_to(REPO_ROOT)
    except ValueError:
        return
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", str(output)],
        cwd=REPO_ROOT,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise EvidenceError("output inside the source checkout must be ignored by Git")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
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
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("real-provider test did not emit a valid proof receipt") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("real-provider proof receipt is not a JSON object")
    return payload


def _validate_receipt(
    receipt: dict[str, Any], *, revision: str, challenge: str
) -> dict[str, Any]:
    expected = {
        "status": "passed",
        "source_revision": revision,
        "backend": "claude",
        "real_provider": True,
        "transport": "http-sse",
        "dependent_turns": 3,
        "turn_1_marker_returned": True,
        "turn_2_retained_turn_1": True,
        "turn_2_marker_returned": True,
        "turn_3_retained_turns_1_and_2": True,
        "provider_processes_remaining": 0,
        "server_processes_remaining": 0,
        "server_stopped_cleanly": True,
        "web_token_removed": True,
        "credential_files_copied": 0,
        "proof_test": TEST_NODE,
        "run_challenge_sha256": hashlib.sha256(challenge.encode("ascii")).hexdigest(),
    }
    for field, value in expected.items():
        if receipt.get(field) != value or type(receipt.get(field)) is not type(value):
            raise EvidenceError(f"real-provider proof receipt has invalid {field}")
    observed = receipt.get("provider_processes_observed")
    if type(observed) is not int or observed < 1:
        raise EvidenceError("real-provider proof receipt did not observe a Claude process")

    result = {
        key: value
        for key, value in expected.items()
        if key != "run_challenge_sha256"
    }
    result["provider_processes_observed"] = observed
    result["evidence_producer"] = "tools/linux_claude_web_evidence.py"
    return result


def produce(*, output: Path, expected_revision: str) -> None:
    output = output.resolve()
    _require_safe_output(output)
    output.unlink(missing_ok=True)

    if not sys.platform.startswith("linux"):
        raise EvidenceError("the credentialed Claude proof requires Linux")
    expected_revision = expected_revision.lower()
    if not REVISION_RE.fullmatch(expected_revision):
        raise EvidenceError("expected revision must be an exact 40-character SHA")
    if _checkout_revision(REPO_ROOT) != expected_revision:
        raise EvidenceError("expected revision does not match the tested checkout")
    _require_clean_checkout(REPO_ROOT)

    challenge = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix="specbutler-linux-claude-proof-") as temp_dir:
        receipt_path = Path(temp_dir) / "receipt.json"
        env = dict(os.environ)
        pythonpath = env.get("PYTHONPATH", "")
        env.update(
            {
                REAL_PROVIDER_ENV: "1",
                RECEIPT_PATH_ENV: str(receipt_path),
                EXPECTED_REVISION_ENV: expected_revision,
                CHALLENGE_ENV: challenge,
                "PYTHONPATH": os.pathsep.join(
                    part for part in (str(REPO_ROOT / "src"), pythonpath) if part
                ),
            }
        )
        # Caller-level pytest options must not deselect, skip, or replace the
        # one exact proof node selected below.
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--strict-markers",
                    "-m",
                    "linux_claude_real_provider",
                    TEST_NODE,
                    "-vv",
                ],
                cwd=REPO_ROOT,
                env=env,
                timeout=2100,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvidenceError("real-provider pytest could not complete") from exc
        if completed.returncode != 0:
            raise EvidenceError(
                f"real-provider pytest failed with exit code {completed.returncode}"
            )
        if not receipt_path.is_file():
            raise EvidenceError("real-provider pytest passed or skipped without a proof receipt")
        receipt = _load_receipt(receipt_path)

    if _checkout_revision(REPO_ROOT) != expected_revision:
        raise EvidenceError("tested checkout revision changed while proof was running")
    _require_clean_checkout(REPO_ROOT)
    result = _validate_receipt(receipt, revision=expected_revision, challenge=challenge)
    _atomic_json(output, result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        produce(output=args.output, expected_revision=args.expected_revision)
    except EvidenceError as exc:
        print(f"Linux Claude evidence failed: {exc}", file=sys.stderr)
        return 1
    print(f"Linux Claude evidence written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
