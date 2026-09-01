"""Resolve the public source repository for the running spec distribution."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .git_common import run_git

PACKAGE_NAME = "specbutler"


def normalize_repository_https_url(raw_url: str) -> str:
    """Return a credential-free HTTPS clone URL, or raise ``ValueError``.

    GitHub-style SCP and SSH clone URLs are accepted for local discovery, but
    generated workers always use public HTTPS so an SSH agent or credential is
    never baked into the image.
    """
    value = str(raw_url).strip()
    if value.startswith("git+"):
        value = value[4:]
    if not value or any(character.isspace() for character in value):
        raise ValueError("repository URL must be a non-empty URL without whitespace")

    scp_match = re.fullmatch(r"[^@/:]+@([^/:]+):(.+)", value)
    if scp_match and "://" not in value:
        host = scp_match.group(1)
        path = scp_match.group(2)
    else:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
            raise ValueError("repository URL must use http(s), ssh, git, or Git SCP syntax")
        if parsed.query or parsed.fragment:
            raise ValueError("repository URL must not include a query or fragment")
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None and parsed.scheme == "https":
            host = f"{host}:{parsed.port}"
        path = parsed.path.lstrip("/")

    path = path.rstrip("/")
    hostname = host.removeprefix("[").split("]", 1)[0]
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", hostname):
            raise ValueError("repository URL must include a valid hostname") from None

    path_parts = [part for part in path.split("/") if part]
    if (
        len(path_parts) < 2
        or path.startswith(".")
        or "/../" in f"/{path}/"
        or any(not re.fullmatch(r"[A-Za-z0-9._~%+-]+", part) for part in path_parts)
    ):
        raise ValueError("repository URL must include an owner and repository path")
    if not path.endswith(".git"):
        path = f"{path}.git"
    return urlunsplit(("https", host.lower(), f"/{path}", "", ""))


def _source_checkout_repository_url() -> str:
    source_root = Path(__file__).resolve().parents[2]
    pyproject = source_root / "pyproject.toml"
    source_module = source_root / "src" / "spec_runtime" / "source_repository.py"
    try:
        if source_module.resolve() != Path(__file__).resolve():
            return ""
        raw = tomllib.loads(pyproject.read_text())
        project = raw.get("project", {})
        if not isinstance(project, dict) or project.get("name") != PACKAGE_NAME:
            return ""
    except (OSError, tomllib.TOMLDecodeError):
        return ""

    try:
        result = run_git(
            ["config", "--get", "remote.origin.url"],
            cwd=source_root,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return normalize_repository_https_url(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

    urls = project.get("urls", {})
    if isinstance(urls, dict):
        for key in ("Repository", "Source"):
            candidate = str(urls.get(key, "")).strip()
            if not candidate:
                continue
            try:
                return normalize_repository_https_url(candidate)
            except ValueError:
                continue
    return ""


def _distribution_repository_url() -> str:
    try:
        dist = metadata.distribution(PACKAGE_NAME)
    except (metadata.PackageNotFoundError, OSError):
        return ""

    try:
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            if isinstance(direct_url, dict):
                candidate = str(direct_url.get("url", "")).strip()
                vcs_info = direct_url.get("vcs_info")
                if (
                    candidate
                    and not candidate.startswith("file:")
                    and isinstance(vcs_info, dict)
                    and str(vcs_info.get("vcs", "")).lower() == "git"
                ):
                    return normalize_repository_https_url(candidate)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass

    package_metadata = getattr(dist, "metadata", None)
    if package_metadata is None:
        return ""
    candidates: list[tuple[str, str]] = []
    get_all = getattr(package_metadata, "get_all", None)
    if callable(get_all):
        for entry in get_all("Project-URL") or get_all("Project-Url") or ():
            label, separator, url = str(entry).partition(",")
            normalized_label = label.strip().lower() if separator else ""
            if normalized_label in {"repository", "source"}:
                candidates.append((normalized_label, url.strip()))
    get = getattr(package_metadata, "get", None)
    if callable(get):
        candidates.append(("repository", str(get("Repository") or "").strip()))

    priority = {"repository": 0, "source": 1}
    for _, candidate in sorted(candidates, key=lambda item: priority.get(item[0], 4)):
        if not candidate:
            continue
        try:
            return normalize_repository_https_url(candidate)
        except ValueError:
            continue
    return ""


def runtime_repository_https_url(*, override: str = "") -> str:
    """Resolve the repository that supplied the running spec package.

    ``override`` is intended for forks and release bootstrapping. Invalid
    explicit values fail loudly; auto-discovery failures return an empty string
    so callers can provide context-specific remediation.
    """
    if str(override).strip():
        return normalize_repository_https_url(override)
    return _source_checkout_repository_url() or _distribution_repository_url()
