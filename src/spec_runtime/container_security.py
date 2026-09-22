"""Explicit, versioned outer policies for nested Linux worker sandboxes."""

from __future__ import annotations

import platform
from pathlib import Path

from .config import ContainerExecutionConfig

NESTED_PROFILE = "nested-v1"
APPARMOR_PROFILE = "specbutler-nested-v1"


def worker_security_args(
    config: ContainerExecutionConfig,
    *,
    system_name: str,
    user_mapping: str,
) -> list[str]:
    """Never relax defaults implicitly, or allow the nested policy with root.

    Docker applies the bundled seccomp policy from its client filesystem and
    requires an administrator to load the named AppArmor profile on the daemon
    host. Missing profiles fail closed at container creation.
    """
    if config.sandbox_profile == "default":
        return []
    if config.sandbox_profile != NESTED_PROFILE:
        raise RuntimeError("Unknown container sandbox profile")
    if (
        system_name != "Linux"
        or platform.machine() not in {"x86_64", "aarch64"}
        or config.engine != "docker"
        or config.compose_file
        or config.playwright_mcp.topology == "sidecar"
    ):
        raise RuntimeError(
            "nested-v1 requires a Linux x86_64/aarch64 Docker coordinator "
            "and in-worker services"
        )
    ids = user_mapping.split(":")
    if len(ids) != 2 or any(not value.isdecimal() or int(value) == 0 for value in ids):
        raise RuntimeError("nested-v1 requires explicit non-root numeric worker uid:gid")
    seccomp = Path(__file__).with_name("profiles") / "specbutler-nested-v1.seccomp.json"
    if not seccomp.is_file():
        raise RuntimeError("The installed nested-v1 seccomp policy is missing")
    return [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        f"--security-opt=apparmor={APPARMOR_PROFILE}",
        f"--security-opt=seccomp={seccomp}",
    ]


def require_same_sandbox_profile(recorded: object, configured: str) -> None:
    if not isinstance(recorded, str) or recorded not in {"default", NESTED_PROFILE}:
        raise RuntimeError("Container state has an invalid sandbox profile")
    if recorded != configured:
        raise RuntimeError(
            "Container backend cannot change sandbox profile while resuming an existing "
            "run; use its original profile or create a new run"
        )
