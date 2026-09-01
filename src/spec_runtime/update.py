from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from packaging.version import InvalidVersion, Version

from .config import SpecRuntimeConfig
from .git_common import resolve_common_root as _resolve_common_root_base
from .source_repository import runtime_repository_https_url

LOGGER = logging.getLogger(__name__)

PACKAGE_NAME = "specbutler"
UPDATE_CACHE_FILENAME = "update-check.json"
UPDATE_CACHE_LOCK_FILENAME = "update-check.lock"
UPDATE_CHECK_TTL = timedelta(hours=24)
UPDATE_REFRESH_LOCK_TTL = timedelta(minutes=10)


def _manual_upgrade_guidance() -> str:
    repository_url = runtime_repository_https_url()
    if repository_url:
        return f'pip install --upgrade "{PACKAGE_NAME} @ git+{repository_url}"'
    return "Reinstall Spec Butler from the repository or package source used for this installation."


@dataclass(frozen=True)
class InstallInfo:
    method: str
    current_version: str
    upgrade_command: tuple[str, ...] = ()
    message: str = ""
    direct_url: dict[str, Any] | None = None
    installer: str = ""
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpdateCacheEntry:
    latest_version: str | None
    checked_at: datetime


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _format_utc(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso8601(value: str) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _normalize_version(version: str) -> str:
    return str(version).strip().removeprefix("v")


def _parse_version(version: str) -> Version | None:
    normalized = _normalize_version(version)
    if not normalized:
        return None
    try:
        return Version(normalized)
    except InvalidVersion:
        return None


def _compare_versions(left: str, right: str) -> int | None:
    left_version = _parse_version(left)
    right_version = _parse_version(right)
    if left_version is None or right_version is None:
        return None
    if left_version < right_version:
        return -1
    if left_version > right_version:
        return 1
    return 0


def _is_newer_version(latest_version: str, current_version: str) -> bool:
    comparison = _compare_versions(latest_version, current_version)
    return comparison is not None and comparison > 0


def _read_distribution(dist_name: str = PACKAGE_NAME) -> metadata.Distribution:
    return metadata.distribution(dist_name)


def _read_json_text(raw_text: str | None) -> dict[str, Any] | None:
    if not raw_text:
        return None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        LOGGER.debug("Could not parse direct_url.json", exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip()).lower()


def _is_pipx_environment(dist_name: str) -> bool:
    """Return whether the running interpreter belongs to this package's pipx venv.

    pipx delegates installation to pip, so a real pipx-managed distribution's
    ``INSTALLER`` file normally contains ``pip``. The venv-level metadata is the
    durable signal that distinguishes it from an ordinary virtual environment.
    """
    metadata_path = Path(sys.prefix) / "pipx_metadata.json"
    try:
        payload = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    main_package = payload.get("main_package")
    if not isinstance(main_package, dict):
        return False
    package = str(main_package.get("package", "")).strip()
    return bool(package) and _normalize_distribution_name(package) == _normalize_distribution_name(dist_name)


def _git_requirement_url(
    direct_url: dict[str, Any],
    *,
    requested_revision: str | None = None,
) -> str | None:
    url = str(direct_url.get("url", "")).strip()
    vcs_info = direct_url.get("vcs_info")
    if not isinstance(vcs_info, dict):
        return None
    vcs = str(vcs_info.get("vcs", "")).strip()
    if not vcs or not url:
        return None
    # Keep credentials in the requirement URL so pip can authenticate to
    # private repos.  Display redaction is handled by _display_command().
    requirement_url = url if url.startswith(f"{vcs}+") else f"{vcs}+{url}"
    # Preserve the original branch/tag/commit unless the caller intentionally
    # advances a tagged stable install to a newer release tag.
    revision = (
        str(vcs_info.get("requested_revision", "")).strip()
        if requested_revision is None
        else requested_revision.strip()
    )
    if revision:
        requirement_url = f"{requirement_url}@{revision}"
    subdirectory = str(direct_url.get("subdirectory", "")).strip()
    if subdirectory:
        requirement_url = f"{requirement_url}#subdirectory={subdirectory}"
    return requirement_url


def _upgrade_command_for_latest_release(
    install_info: InstallInfo,
    latest_version: str | None,
) -> tuple[str, ...]:
    """Advance version-tag VCS installs while preserving branch/commit channels."""
    if not latest_version or not install_info.direct_url:
        return install_info.upgrade_command
    vcs_info = install_info.direct_url.get("vcs_info")
    if not isinstance(vcs_info, dict):
        return install_info.upgrade_command
    requested = str(vcs_info.get("requested_revision", "")).strip()
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", requested):
        return install_info.upgrade_command

    prefix = "v" if requested.startswith("v") else ""
    requirement_url = _git_requirement_url(
        install_info.direct_url,
        requested_revision=f"{prefix}{_normalize_version(latest_version)}",
    )
    if not requirement_url:
        return install_info.upgrade_command
    requirement = f"{PACKAGE_NAME} @ {requirement_url}"
    if install_info.method == "pipx":
        # pipx's stored package spec still names the old immutable tag. Run pip
        # inside the managed environment so existing optional dependencies are
        # retained while the core package advances to the latest release tag.
        return ("pipx", "runpip", PACKAGE_NAME, "install", "--upgrade", requirement)
    if "uv" in install_info.installer:
        return ("uv", "pip", "install", "--upgrade", requirement)
    return _pip_command("install", "--upgrade", requirement)


def _distribution_source_urls(dist: metadata.Distribution) -> tuple[str, ...]:
    package_metadata = getattr(dist, "metadata", None)
    if package_metadata is None:
        return ()

    urls: list[str] = []
    seen: set[str] = set()

    def add_url(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            urls.append(value)

    metadata_get = getattr(package_metadata, "get", None)
    if callable(metadata_get):
        for key in ("Home-page", "Download-URL"):
            value = str(metadata_get(key) or "").strip()
            add_url(value)

    metadata_get_all = getattr(package_metadata, "get_all", None)
    if callable(metadata_get_all):
        for key in ("Project-URL", "Project-Url"):
            for raw_value in metadata_get_all(key) or ():
                text = str(raw_value).strip()
                if not text:
                    continue
                _, _, url = text.partition(",")
                candidate = (url or text).strip()
                add_url(candidate)

    return tuple(urls)


def _pip_command(*args: str) -> tuple[str, ...]:
    python_executable = sys.executable or "python3"
    # -I (isolated mode) prevents sys.path from including cwd, so a
    # repo-local pip.py cannot shadow the real pip module.
    return (python_executable, "-I", "-m", "pip", *args)


def _split_url_revision(url: str) -> tuple[str, str]:
    scheme_index = url.find("://")
    if scheme_index == -1:
        return url, ""

    path_index = url.find("/", scheme_index + 3)
    if path_index == -1:
        return url, ""

    revision_index = url.rfind("@", path_index)
    if revision_index == -1:
        return url, ""
    return url[:revision_index], url[revision_index:]


def _redact_url_credentials(url: str) -> str:
    raw_url = str(url).strip()
    if not raw_url or "://" not in raw_url:
        return raw_url

    base_url, revision = _split_url_revision(raw_url)
    parsed = urllib_parse.urlsplit(base_url)
    if not parsed.netloc or (parsed.username is None and parsed.password is None):
        return raw_url

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    host = hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"

    sanitized = parsed._replace(netloc=f"***@{host}")
    return urllib_parse.urlunsplit(sanitized) + revision


def _strip_url_credentials(url: str) -> str:
    """Remove embedded credentials (username:password) from a URL.

    Only strips when a password component is present (e.g. ``oauth2:token@host``),
    preserving plain SSH usernames like ``git@host``.
    """
    raw_url = str(url).strip()
    if not raw_url or "://" not in raw_url:
        return raw_url
    parsed = urllib_parse.urlsplit(raw_url)
    if not parsed.password:
        return raw_url
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    host = hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    sanitized = parsed._replace(netloc=host)
    return urllib_parse.urlunsplit(sanitized)


def _redact_command_argument(argument: str) -> str:
    value = str(argument)
    if " @ " in value:
        requirement, url = value.split(" @ ", 1)
        return f"{requirement} @ {_redact_url_credentials(url)}"
    return _redact_url_credentials(value)


def _display_command(command: tuple[str, ...]) -> str:
    return shlex.join(tuple(_redact_command_argument(arg) for arg in command))


def detect_installation(dist_name: str = PACKAGE_NAME) -> InstallInfo:
    dist = _read_distribution(dist_name)
    current_version = metadata.version(dist_name)
    direct_url = _read_json_text(dist.read_text("direct_url.json"))
    installer = str(dist.read_text("INSTALLER") or "").strip().lower()
    source_urls = _distribution_source_urls(dist)

    if direct_url and isinstance(direct_url.get("dir_info"), dict) and direct_url["dir_info"].get("editable"):
        return InstallInfo(
            method="editable",
            current_version=current_version,
            message="Editable install — pull and reinstall manually",
            direct_url=direct_url,
            installer=installer,
            source_urls=source_urls,
        )

    if "pipx" in installer or _is_pipx_environment(dist_name):
        return InstallInfo(
            method="pipx",
            current_version=current_version,
            upgrade_command=("pipx", "upgrade", dist_name),
            direct_url=direct_url,
            installer=installer,
            source_urls=source_urls,
        )

    if direct_url and isinstance(direct_url.get("vcs_info"), dict):
        requirement_url = _git_requirement_url(direct_url)
        if requirement_url:
            upgrade_command = _pip_command("install", "--upgrade", f"{dist_name} @ {requirement_url}")
            if "uv" in installer:
                upgrade_command = ("uv", "pip", "install", "--upgrade", f"{dist_name} @ {requirement_url}")
            return InstallInfo(
                method="git",
                current_version=current_version,
                upgrade_command=upgrade_command,
                direct_url=direct_url,
                installer=installer,
                source_urls=source_urls,
            )

    if "uv" in installer:
        return InstallInfo(
            method="uv",
            current_version=current_version,
            upgrade_command=("uv", "pip", "install", "--upgrade", dist_name),
            direct_url=direct_url,
            installer=installer,
            source_urls=source_urls,
        )

    if direct_url is None:
        return InstallInfo(
            method="pypi",
            current_version=current_version,
            upgrade_command=_pip_command("install", "--upgrade", dist_name),
            direct_url=None,
            installer=installer,
            source_urls=source_urls,
        )

    return InstallInfo(
        method="unknown",
        current_version=current_version,
        message=(f"Could not determine install method. Update manually with:\n{_manual_upgrade_guidance()}"),
        direct_url=direct_url,
        installer=installer,
        source_urls=source_urls,
    )


def _parse_github_repo(url: str) -> str | None:
    cleaned = str(url).strip()
    if not cleaned:
        return None

    if cleaned.startswith("git+"):
        cleaned = cleaned[4:]

    if cleaned.startswith("git@github.com:"):
        tail = cleaned.removeprefix("git@github.com:")
        return tail[:-4] if tail.endswith(".git") else tail

    parsed = urllib_parse.urlparse(cleaned)
    if parsed.scheme and (parsed.hostname or "").lower() == "github.com":
        path = parsed.path.strip("/")
        return path[:-4] if path.endswith(".git") else path

    return None


def _origin_remote_url(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def resolve_repo_slug(
    repo_root: Path,
    direct_url: dict[str, Any] | None = None,
    *,
    source_urls: tuple[str, ...] = (),
) -> str | None:
    # Resolve the package's source repository slug (owner/repo).
    # For non-editable installs the package metadata (direct_url, source_urls)
    # is authoritative — it points at the Spec Butler repo regardless of
    # which project the user is working in.  The current checkout's origin
    # remote is only useful for editable/local installs where the metadata
    # carries a file:// URL.

    # 1. VCS URL from direct_url.json (primary for non-editable installs).
    is_local_install = False
    if direct_url:
        direct_url_str = str(direct_url.get("url", "")).strip()
        parsed_direct = _parse_github_repo(direct_url_str)
        if parsed_direct:
            return parsed_direct
        is_local_install = bool(direct_url_str) and urllib_parse.urlparse(direct_url_str).scheme == "file"
        if direct_url_str and not is_local_install:
            LOGGER.debug(
                "Could not parse GitHub owner/repo from direct_url.json URL: %s",
                _redact_url_credentials(direct_url_str),
            )

    # 2. For local/editable installs, prefer the current checkout's origin over
    #    package metadata URLs — metadata may point at the upstream repo rather
    #    than the active fork.
    if is_local_install:
        origin_url = _origin_remote_url(repo_root)
        if origin_url:
            parsed_origin = _parse_github_repo(origin_url)
            if parsed_origin:
                return parsed_origin
            LOGGER.debug(
                "Could not parse GitHub owner/repo from current repo origin URL: %s",
                _redact_url_credentials(origin_url),
            )

    # 3. Source URLs from distribution metadata (Home-page, Project-URL, etc.).
    for source_url in source_urls:
        parsed_source = _parse_github_repo(source_url)
        if parsed_source:
            return parsed_source
        if source_url:
            LOGGER.debug(
                "Could not parse GitHub owner/repo from package metadata URL: %s", _redact_url_credentials(source_url)
            )

    # 4. Current repository's origin remote (fallback when metadata unavailable).
    if not is_local_install:
        origin_url = _origin_remote_url(repo_root)
        if origin_url:
            parsed_origin = _parse_github_repo(origin_url)
            if parsed_origin:
                return parsed_origin
            LOGGER.debug(
                "Could not parse GitHub owner/repo from current repo origin URL: %s",
                _redact_url_credentials(origin_url),
            )

    LOGGER.debug("Skipping update version check because no repository URL was available.")
    return None


def _github_token() -> str:
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def fetch_latest_version(
    repo_slug: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
    include_prereleases: bool = False,
) -> str | None:
    if not repo_slug:
        return None

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": PACKAGE_NAME,
    }
    auth_token = token if token is not None else _github_token()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    best_tag = ""
    best_version: Version | None = None
    page = 1
    per_page = 100
    while True:
        query = urllib_parse.urlencode({"per_page": per_page, "page": page})
        url = f"https://api.github.com/repos/{urllib_parse.quote(repo_slug, safe='/')}/releases?{query}"
        request = urllib_request.Request(url, headers=headers)
        try:
            with urllib_request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib_error.URLError, urllib_error.HTTPError, json.JSONDecodeError):
            LOGGER.debug("Failed to fetch latest releases for %s", repo_slug, exc_info=True)
            return None

        if not isinstance(payload, list):
            return None
        if not payload:
            break

        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("draft") is True:
                continue
            if not include_prereleases and item.get("prerelease") is True:
                continue
            tag_name = str(item.get("tag_name", "")).strip()
            parsed_version = _parse_version(tag_name)
            if parsed_version is None:
                continue
            if not include_prereleases and parsed_version.is_prerelease:
                continue
            if best_version is None or parsed_version > best_version:
                best_version = parsed_version
                best_tag = tag_name

        if len(payload) < per_page:
            break
        page += 1

    return _normalize_version(best_tag) if best_tag else None


def _discover_repo_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return None


def resolve_common_root(repo_root: Path) -> Path:
    """Resolve common root with an additional ``--show-toplevel`` validation.

    The extra check ensures *repo_root* is actually the toplevel of its
    worktree before trusting the git-common-dir result.  If not, fall back
    to the filesystem-based heuristic.
    """
    from .git_common import _resolve_common_root_fallback

    fallback_root = repo_root.resolve()
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _resolve_common_root_fallback(fallback_root)
    if toplevel.returncode != 0 or not toplevel.stdout.strip():
        return _resolve_common_root_fallback(fallback_root)

    try:
        resolved_toplevel = Path(toplevel.stdout.strip()).resolve()
    except OSError:
        return _resolve_common_root_fallback(fallback_root)
    if resolved_toplevel != fallback_root:
        return _resolve_common_root_fallback(fallback_root)

    return _resolve_common_root_base(repo_root)


def update_cache_path(repo_root: Path, config: SpecRuntimeConfig) -> Path:
    return resolve_common_root(repo_root) / config.paths.state_dir / UPDATE_CACHE_FILENAME


def update_cache_lock_path(repo_root: Path, config: SpecRuntimeConfig) -> Path:
    return resolve_common_root(repo_root) / config.paths.state_dir / UPDATE_CACHE_LOCK_FILENAME


def read_update_cache(cache_path: Path) -> UpdateCacheEntry | None:
    try:
        payload = json.loads(cache_path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    raw_latest_version = payload.get("latest_version")
    latest_version = None
    if raw_latest_version is not None:
        latest_version = _normalize_version(str(raw_latest_version).strip()) or None
    checked_at = _parse_iso8601(str(payload.get("checked_at", "")).strip())
    if checked_at is None:
        return None
    return UpdateCacheEntry(latest_version=latest_version, checked_at=checked_at)


def write_update_cache(cache_path: Path, latest_version: str | None, *, checked_at: datetime | None = None) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "latest_version": _normalize_version(latest_version) if latest_version is not None else None,
        "checked_at": _format_utc(checked_at or _now_utc()),
    }
    cache_path.write_text(json.dumps(payload, indent=2) + "\n")


def _parse_refresh_lock(lock_path: Path) -> datetime | None:
    try:
        payload = json.loads(lock_path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_iso8601(str(payload.get("started_at", "")).strip())


def _write_refresh_lock(lock_path: Path, *, started_at: datetime) -> bool:
    payload = json.dumps({"started_at": _format_utc(started_at)}, indent=2) + "\n"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        LOGGER.debug("Failed to create update refresh lock %s", lock_path, exc_info=True)
        return False

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError:
        LOGGER.debug("Failed to write update refresh lock %s", lock_path, exc_info=True)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.debug("Failed to clean up update refresh lock %s", lock_path, exc_info=True)
        return False
    return True


def _claim_refresh_lock(lock_path: Path) -> bool:
    now = _now_utc()
    if _write_refresh_lock(lock_path, started_at=now):
        return True

    started_at = _parse_refresh_lock(lock_path)
    if started_at is not None and now - started_at <= UPDATE_REFRESH_LOCK_TTL:
        return False

    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return _write_refresh_lock(lock_path, started_at=now)


def _release_refresh_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOGGER.debug("Failed to remove update refresh lock %s", lock_path, exc_info=True)


def cache_is_fresh(entry: UpdateCacheEntry, *, now: datetime | None = None) -> bool:
    current_time = now or _now_utc()
    return current_time - entry.checked_at <= UPDATE_CHECK_TTL


def refresh_update_cache(
    repo_root: Path, config: SpecRuntimeConfig, *, install_info: InstallInfo | None = None
) -> None:
    cache_path = update_cache_path(repo_root, config)
    repo_slug = resolve_repo_slug(
        repo_root,
        install_info.direct_url if install_info else None,
        source_urls=install_info.source_urls if install_info else (),
    )
    if not repo_slug:
        write_update_cache(cache_path, None)
        return
    include_prereleases = False
    if install_info:
        current = _parse_version(install_info.current_version)
        if current and current.is_prerelease:
            include_prereleases = True
    latest_version = fetch_latest_version(repo_slug, include_prereleases=include_prereleases)
    write_update_cache(cache_path, latest_version)


def _background_refresh_entry(repo_root: Path, cache_path: Path, lock_path: Path) -> None:
    """Entry point for subprocess-based background cache refresh.

    Called in a detached subprocess so the refresh survives parent process exit.
    The caller must hold the lock before spawning this entry point.
    """
    try:
        install_info = detect_installation()
        repo_slug = resolve_repo_slug(
            repo_root,
            install_info.direct_url if install_info else None,
            source_urls=install_info.source_urls if install_info else (),
        )
        if not repo_slug:
            write_update_cache(cache_path, None)
        else:
            include_prereleases = False
            current = _parse_version(install_info.current_version)
            if current and current.is_prerelease:
                include_prereleases = True
            latest_version = fetch_latest_version(repo_slug, include_prereleases=include_prereleases)
            write_update_cache(cache_path, latest_version)
    except Exception:
        pass
    finally:
        _release_refresh_lock(lock_path)


def _start_refresh_subprocess(repo_root: Path, cache_path: Path, lock_path: Path) -> None:
    """Launch a detached subprocess to refresh the update cache.

    The subprocess survives parent process exit so short-lived CLI
    commands do not kill the refresh mid-write.
    """
    script = (
        "import sys; from pathlib import Path; "
        "from spec_runtime.update import _background_refresh_entry; "
        "_background_refresh_entry(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))"
    )
    from .platform_fs import atomic_write_text
    from .process_supervisor import LifetimeMode, ProcessSupervisor

    managed = ProcessSupervisor(LifetimeMode.DETACHED).spawn(
        [sys.executable, "-I", "-c", script, str(repo_root), str(cache_path), str(lock_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd="/",
    )
    token_path = lock_path.with_suffix(lock_path.suffix + ".supervision.json")
    atomic_write_text(token_path, json.dumps(managed.token.to_dict(), sort_keys=True) + "\n")


def _spawn_cache_refresh(repo_root: Path, config: SpecRuntimeConfig) -> None:
    lock_path = update_cache_lock_path(repo_root, config)
    if not _claim_refresh_lock(lock_path):
        return

    cache_path = update_cache_path(repo_root, config)
    try:
        _start_refresh_subprocess(repo_root, cache_path, lock_path)
    except Exception:
        _release_refresh_lock(lock_path)
        LOGGER.debug("Failed to start background update check", exc_info=True)


def maybe_print_update_notice(repo_root: Path, config: SpecRuntimeConfig) -> None:
    if os.getenv("SPEC_NO_UPDATE_CHECK", "").strip() == "1":
        return
    if not config.update.check_enabled:
        return

    cache_path = update_cache_path(repo_root, config)
    cache_entry = read_update_cache(cache_path)
    if cache_entry is None or not cache_is_fresh(cache_entry):
        _spawn_cache_refresh(repo_root, config)
        return

    try:
        current_version = metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return
    if cache_entry.latest_version and _is_newer_version(cache_entry.latest_version, current_version):
        print(
            f"Update available: v{_normalize_version(current_version)} \u2192 "
            f'v{cache_entry.latest_version}. Run "spec update" to upgrade.',
            file=os.sys.stderr,
        )


# Bundled template → repo-local destination mapping for drift detection.
_TEMPLATE_DESTINATIONS: tuple[tuple[str, str], ...] = (
    ("TEMPLATE.md", "specs/TEMPLATE.md"),
    ("review.md", ".github/prompts/review.md"),
    ("AGENTS.md", "AGENTS.md"),
    ("CLAUDE.md", "CLAUDE.md"),
    ("CODEX.md", "CODEX.md"),
)


def _read_current_templates() -> dict[str, str]:
    """Read bundled template contents from the currently-loaded package.

    Call this *before* the upgrade so we have a snapshot of the old templates.
    """
    from spec_runtime.init import _read_bundled_template  # noqa: PLC0415

    templates: dict[str, str] = {}
    for template_name, _ in _TEMPLATE_DESTINATIONS:
        try:
            templates[template_name] = _read_bundled_template(template_name)
        except Exception:
            pass
    return templates


def _check_template_drift(repo_root: Path, old_templates: dict[str, str]) -> bool:
    """Check if any bundled templates changed in the upgrade and repo copies are stale.

    Compares pre-upgrade bundled templates (*old_templates*) against post-upgrade
    bundled templates (read via subprocess since the current process still has old
    modules cached).  Only reports drift when a template actually changed in the
    upgrade AND the repo-local copy doesn't match the new version.
    """
    for template_name, local_rel_path in _TEMPLATE_DESTINATIONS:
        local_path = repo_root / local_rel_path
        if not local_path.exists():
            continue
        try:
            new_bundled = subprocess.check_output(
                [
                    sys.executable, "-I", "-c",
                    f"from spec_runtime.init import _read_bundled_template; print(_read_bundled_template({template_name!r}), end='')",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            continue
        # If the bundled template didn't change in this upgrade, skip it —
        # any difference is a pre-existing user customization, not new drift.
        old_bundled = old_templates.get(template_name)
        if old_bundled is not None and old_bundled == new_bundled:
            continue
        try:
            local_content = local_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if new_bundled != local_content:
            return True
    return False


def cmd_update(_args: Any) -> int:
    try:
        install_info = detect_installation()
    except metadata.PackageNotFoundError:
        print(
            "Spec Butler is not installed in this environment.",
            file=os.sys.stderr,
        )
        return 1

    if install_info.method == "editable":
        print(install_info.message)
        return 0

    if install_info.method == "unknown" or not install_info.upgrade_command:
        print(install_info.message or _manual_upgrade_guidance(), file=os.sys.stderr)
        return 1

    repo_root = _discover_repo_root()
    repo_slug = resolve_repo_slug(repo_root or Path.cwd(), install_info.direct_url, source_urls=install_info.source_urls)
    current_parsed = _parse_version(install_info.current_version)
    include_prereleases = current_parsed is not None and current_parsed.is_prerelease
    latest_version = fetch_latest_version(repo_slug, include_prereleases=include_prereleases) if repo_slug else None
    if latest_version and not _is_newer_version(latest_version, install_info.current_version):
        print(f"Already on the latest version (v{_normalize_version(install_info.current_version)})")
        return 0

    # Snapshot current bundled templates before the upgrade so we can detect
    # which templates actually changed (avoids false positives from pre-existing
    # user customizations).
    old_templates = _read_current_templates() if repo_root is not None else {}

    upgrade_command = _upgrade_command_for_latest_release(
        install_info,
        latest_version,
    )
    print(_display_command(upgrade_command))
    # Run from a safe cwd (/) so repo-local modules (e.g. pip.py) cannot
    # shadow real packages via sys.path including ".".
    completed = subprocess.run(upgrade_command, check=False, cwd="/")
    if completed.returncode == 0:
        try:
            installed_version = metadata.version(PACKAGE_NAME)
        except metadata.PackageNotFoundError:
            installed_version = latest_version or install_info.current_version
        new_version = _normalize_version(installed_version)
        print(f"Updated Spec Butler from v{_normalize_version(install_info.current_version)} to v{new_version}")
        if new_version != _normalize_version(install_info.current_version) and repo_root is not None:
            try:
                if _check_template_drift(repo_root, old_templates):
                    print(
                        'Templates have changed. Run "spec init --force" to refresh them.',
                        file=sys.stderr,
                    )
            except Exception:
                LOGGER.debug("Template drift check failed", exc_info=True)
    return completed.returncode
