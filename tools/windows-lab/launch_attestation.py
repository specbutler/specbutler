#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AttestationError(ValueError):
    pass


def _load_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AttestationError(f"invalid launch evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AttestationError(f"launch evidence must be an object: {path}")
    return raw, payload


def _validate_receipt(
    receipt: dict[str, Any], *, expected_job: str, expected_nonce: str
) -> None:
    if receipt.get("job") != expected_job:
        raise AttestationError("launch receipt belongs to another job")
    if receipt.get("launch_nonce") != expected_nonce:
        raise AttestationError("launch receipt does not match the exact host nonce")
    if not isinstance(receipt.get("session_id"), int) or receipt["session_id"] <= 0:
        raise AttestationError("launch receipt has an invalid desktop session")
    for key in ("user_name", "user_sid", "started_at"):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise AttestationError(f"launch receipt field {key!r} is invalid")


def capture_attestation(
    receipt_path: Path,
    output_path: Path,
    *,
    expected_job: str,
    expected_nonce: str,
) -> dict[str, Any]:
    if output_path.exists():
        raise AttestationError(f"launch attestation already exists: {output_path}")
    if len(expected_nonce) != 32 or any(
        char not in "0123456789abcdef" for char in expected_nonce
    ):
        raise AttestationError("expected launch nonce must be 32 lowercase hex characters")
    raw, receipt = _load_object(receipt_path)
    _validate_receipt(
        receipt, expected_job=expected_job, expected_nonce=expected_nonce
    )
    payload = {
        "status": "captured-before-release",
        "job": expected_job,
        "expected_nonce": expected_nonce,
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "receipt": receipt,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(output_path)
    return payload


def validate_attestation(
    attestation_path: Path,
    receipt_path: Path,
    user_context_path: Path,
    *,
    expected_job: str,
) -> dict[str, Any]:
    receipt_raw, receipt = _load_object(receipt_path)
    _, attestation = _load_object(attestation_path)
    _, user_context = _load_object(user_context_path)
    if attestation.get("status") != "captured-before-release":
        raise AttestationError("host launch attestation was not captured before release")
    if attestation.get("job") != expected_job:
        raise AttestationError("host launch attestation belongs to another job")
    expected_nonce = attestation.get("expected_nonce")
    if not isinstance(expected_nonce, str) or len(expected_nonce) != 32 or any(
        char not in "0123456789abcdef" for char in expected_nonce
    ):
        raise AttestationError("host launch attestation has an invalid expected nonce")
    _validate_receipt(
        receipt, expected_job=expected_job, expected_nonce=expected_nonce
    )
    if attestation.get("receipt") != receipt:
        raise AttestationError("retained host and guest launch receipts differ")
    if attestation.get("receipt_sha256") != hashlib.sha256(receipt_raw).hexdigest():
        raise AttestationError("launch receipt hash does not match host attestation")
    for key in ("user_name", "user_sid", "session_id"):
        if receipt.get(key) != user_context.get(key):
            raise AttestationError(
                f"launch receipt field {key!r} does not match proof context"
            )
    return attestation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--receipt", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--expected-job", required=True)
    capture.add_argument("--expected-nonce", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--attestation", type=Path, required=True)
    validate.add_argument("--receipt", type=Path, required=True)
    validate.add_argument("--user-context", type=Path, required=True)
    validate.add_argument("--expected-job", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "capture":
            capture_attestation(
                args.receipt,
                args.output,
                expected_job=args.expected_job,
                expected_nonce=args.expected_nonce,
            )
        else:
            validate_attestation(
                args.attestation,
                args.receipt,
                args.user_context,
                expected_job=args.expected_job,
            )
    except AttestationError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
