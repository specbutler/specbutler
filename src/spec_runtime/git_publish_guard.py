"""Launch-scoped Git configuration that reserves publication for the host."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import MutableMapping
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from .provider_env import MODEL_PROVIDER_ENV_KEYS

_CONFIG_ENV_RE = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_\d+\Z")
_URL_CANDIDATE_RE = re.compile(r"(?:https?|ssh|git|file)://[^\s\0]+", re.IGNORECASE)
_SCP_SECRET_RE = re.compile(r"[^/@\s]*:[^/@\s]+@[^/:\s]+:")
_SCP_REMOTE_RE = re.compile(
    r"^(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/:\s]+):(?P<path>[^\s]+)$"
)
_REMOTE_HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_PUBLICATION_URL_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})

# pushInsteadOf is applied only to push URLs, so fetch/pull keep their normal
# transport. The synthetic protocol has no registered remote helper and fails
# before Git can contact a credential helper or remote endpoint.
_PUSH_PREFIXES = (
    ("https", "https://"),
    ("http", "http://"),
    ("ssh", "ssh://"),
    ("git", "git://"),
    ("scp", "git@"),
    ("file", "file://"),
)


class UnsafeRepositoryGitConfigError(RuntimeError):
    """Raised when repository-local Git config exposes operator credentials."""


def _git_inspection_environment() -> dict[str, str]:
    """Return an environment that cannot redirect repository inspection."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            if value := os.environ.get(name):
                env[name] = value
    return env


def _local_git_config(repo_root: Path) -> tuple[tuple[str, str], ...]:
    """Read only repository-local config without expanding include directives."""
    if not (repo_root / ".git").exists():
        return ()

    try:
        completed = subprocess.run(
            ["git", "config", "--local", "--null", "--list", "--no-includes"],
            cwd=repo_root,
            env=_git_inspection_environment(),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsafeRepositoryGitConfigError(
            "Unable to inspect repository-local Git config before agent launch."
        ) from exc
    if completed.returncode != 0:
        raise UnsafeRepositoryGitConfigError(
            "Unable to inspect repository-local Git config before agent launch."
        )

    entries: list[tuple[str, str]] = []
    for raw_entry in completed.stdout.split(b"\0"):
        if not raw_entry:
            continue
        raw_key, separator, raw_value = raw_entry.partition(b"\n")
        if not separator:
            raise UnsafeRepositoryGitConfigError(
                "Repository-local Git config returned an invalid record."
            )
        entries.append(
            (
                raw_key.decode("utf-8", errors="replace"),
                raw_value.decode("utf-8", errors="replace"),
            )
        )
    worktree_config = _worktree_git_config_path(repo_root)
    if worktree_config is not None and worktree_config.is_symlink():
        raise UnsafeRepositoryGitConfigError(
            "Repository worktree Git config must not be a symbolic link."
        )
    if worktree_config is not None and worktree_config.exists():
        entries.extend(_git_config_file_entries(worktree_config))
    return tuple(entries)


def _worktree_git_config_path(repo_root: Path) -> Path | None:
    """Locate the per-worktree config without honoring Git config redirects."""
    dot_git = repo_root / ".git"
    if dot_git.is_symlink():
        raise UnsafeRepositoryGitConfigError(
            "Repository .git entry must not be a symbolic link."
        )
    if dot_git.is_dir():
        return dot_git / "config.worktree"
    if not dot_git.is_file():
        return None
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UnsafeRepositoryGitConfigError(
            "Unable to inspect repository worktree Git config before agent launch."
        ) from exc
    prefix = "gitdir: "
    if not marker.startswith(prefix) or "\n" in marker:
        raise UnsafeRepositoryGitConfigError(
            "Repository .git pointer is invalid; refusing agent or forge access."
        )
    git_dir = Path(marker.removeprefix(prefix))
    if not git_dir.is_absolute():
        git_dir = dot_git.parent / git_dir
    return git_dir.resolve(strict=False) / "config.worktree"


def _git_config_file_entries(config_path: Path) -> tuple[tuple[str, str], ...]:
    """Read a specific config file without includes or ambient Git settings."""
    try:
        completed = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(config_path),
                "--null",
                "--list",
                "--no-includes",
            ],
            env=_git_inspection_environment(),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsafeRepositoryGitConfigError(
            "Unable to inspect repository worktree Git config before agent launch."
        ) from exc
    if completed.returncode != 0:
        raise UnsafeRepositoryGitConfigError(
            "Unable to inspect repository worktree Git config before agent launch."
        )
    entries: list[tuple[str, str]] = []
    for raw_entry in completed.stdout.split(b"\0"):
        if not raw_entry:
            continue
        raw_key, separator, raw_value = raw_entry.partition(b"\n")
        if not separator:
            raise UnsafeRepositoryGitConfigError(
                "Repository worktree Git config returned an invalid record."
            )
        # Source-tag these records so moving a setting between the shared and
        # worktree files cannot preserve the publication fingerprint while
        # changing Git's precedence semantics.
        entries.append(
            (
                f"worktree::{raw_key.decode('utf-8', errors='replace')}",
                raw_value.decode("utf-8", errors="replace"),
            )
        )
    return tuple(entries)


def _contains_url_userinfo(value: str) -> bool:
    for match in _URL_CANDIDATE_RE.finditer(value):
        try:
            parsed = urlsplit(match.group(0))
            if parsed.password is not None:
                return True
            if (
                parsed.scheme.casefold() in {"http", "https"}
                and parsed.username is not None
            ):
                return True
        except ValueError:
            # A malformed URL-like value is not safe to expose or hand to Git.
            return True
    # Ordinary ``git@host:path`` SSH remotes contain only a public login name.
    # Reject the password-shaped ``login:secret@host:path`` variant.
    return _SCP_SECRET_RE.search(value) is not None


def _assert_safe_publication_remote_url(remote_url: str) -> str:
    """Return a host-safe remote operand or fail without echoing its value.

    Publication remotes are later passed to trusted host-side ``git fetch``
    and ``git push`` commands.  Git accepts both option-shaped operands and
    ``<transport>::<address>`` remote-helper syntax, so a repository-selected
    value must not reach those commands merely because it contains no
    credential.  Ordinary URL, SCP-style, absolute, and relative repository
    paths remain supported.
    """
    cleaned = remote_url.strip()
    unsafe = (
        not cleaned
        or cleaned != remote_url
        or cleaned.startswith("-")
        or _REMOTE_HELPER_RE.match(cleaned) is not None
        or any(ord(character) < 32 or ord(character) == 127 for character in cleaned)
    )
    if "://" in cleaned:
        try:
            parsed = urlsplit(cleaned)
        except ValueError:
            unsafe = True
        else:
            unsafe = (
                unsafe
                or parsed.scheme.casefold() not in _PUBLICATION_URL_SCHEMES
                or (parsed.hostname or "").startswith("-")
            )
    else:
        scp_match = _SCP_REMOTE_RE.fullmatch(cleaned)
        if scp_match is not None:
            user = scp_match.group("user") or ""
            host = scp_match.group("host") or ""
            unsafe = unsafe or user.startswith("-") or host.startswith("-")
    if unsafe:
        raise UnsafeRepositoryGitConfigError(
            "Repository remote.origin.url is not safe for host Git publication. "
            "Use an ordinary credential-free URL, SCP-style SSH remote, or "
            "local repository path."
        )
    return cleaned


def assert_no_agent_visible_git_credentials(repo_root: Path) -> None:
    """Reject repository-local Git settings that may disclose credentials.

    A linked worktree exposes its shared repository config to agent-selected
    file reads. Launch-only Git overrides can prevent publication, but they
    cannot redact secrets already stored in that file. Refuse the launch
    without echoing values when a credential-bearing setting is present.
    """
    for key, value in _local_git_config(repo_root):
        normalized = key.removeprefix("worktree::").casefold()
        unsafe_reason: str | None = None
        key_contains_secret = _contains_url_userinfo(key)
        if key_contains_secret or _contains_url_userinfo(value):
            unsafe_reason = "URL user information"
        elif normalized == "include.path" or normalized.startswith("includeif."):
            unsafe_reason = "include directive"
        elif normalized == "credential.helper" or normalized.startswith(
            "credential."
        ) and normalized.endswith(".helper"):
            unsafe_reason = "credential helper"
        elif normalized.startswith("http.") and normalized.endswith(".extraheader"):
            header_name = value.partition(":")[0].strip().casefold()
            if header_name in {"authorization", "proxy-authorization"}:
                unsafe_reason = "authorization header"
        if unsafe_reason is not None:
            raise UnsafeRepositoryGitConfigError(
                "Repository-local Git config contains an agent-visible "
                f"{unsafe_reason}. Remove it from local config and "
                "use the host credential store before launching an agent."
            )


def capture_repository_publication_baseline(repo_root: Path) -> tuple[str, str]:
    """Capture the credential-free remote and immutable local-config digest."""
    assert_no_agent_visible_git_credentials(repo_root)
    entries = _local_git_config(repo_root)
    remote_urls = [
        value
        for key, value in entries
        if key.casefold() == "remote.origin.url"
    ]
    if len(remote_urls) != 1:
        raise UnsafeRepositoryGitConfigError(
            "Repository must have exactly one credential-free remote.origin.url "
            "before an agent can run."
        )
    remote_url = _assert_safe_publication_remote_url(remote_urls[0])
    stable_entries = [
        (key, value)
        for key, value in entries
        # Host push with -u may update branch tracking. It cannot influence a
        # direct-URL push, so exclude only this Git-managed namespace.
        if not key.casefold().startswith("branch.")
    ]
    digest = sha256(
        "".join(f"{key}\0{value}\0" for key, value in stable_entries).encode("utf-8")
    ).hexdigest()
    return remote_url, digest


def github_repo_slug_from_remote_url(remote_url: str) -> str:
    """Return the trusted ``OWNER/REPO`` slug for a github.com remote.

    The returned value is suitable for ``GH_REPO``.  Empty means the remote
    is not an unambiguous GitHub URL and must not be used by the built-in
    GitHub forge adapter after an agent has run.
    """
    cleaned = remote_url.strip()
    if not cleaned or _contains_url_userinfo(cleaned):
        return ""

    host = ""
    path = ""
    scp_match = _SCP_REMOTE_RE.fullmatch(cleaned)
    if scp_match and "://" not in cleaned:
        host = scp_match.group("host")
        path = scp_match.group("path")
    else:
        try:
            parsed = urlsplit(cleaned)
        except ValueError:
            return ""
        if parsed.scheme.casefold() not in {"https", "ssh", "git"}:
            return ""
        if parsed.username not in {None, "git"} or parsed.password is not None:
            return ""
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")

    host = host.casefold().rstrip(".")
    if host != "github.com":
        return ""
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part or "") for part in parts
    ):
        return ""
    owner_repo = "/".join(parts)
    return owner_repo


def assert_repository_publication_baseline(
    repo_root: Path,
    *,
    expected_remote_url: str,
    expected_config_fingerprint: str,
) -> None:
    """Fail closed if agent-writable Git metadata changed after launch."""
    current_remote_url, current_fingerprint = capture_repository_publication_baseline(
        repo_root
    )
    if current_remote_url != expected_remote_url:
        raise UnsafeRepositoryGitConfigError(
            "Repository remote changed after the trusted publication baseline was captured."
        )
    if current_fingerprint != expected_config_fingerprint:
        raise UnsafeRepositoryGitConfigError(
            "Repository-local Git config changed after the trusted publication "
            "baseline was captured. Inspect and restore .git/config from an "
            "operator shell before publishing."
        )


def host_publication_git_environment() -> dict[str, str]:
    """Return a host Git environment without agent-selected redirects or hooks."""
    env = dict(os.environ)
    for name in tuple(env):
        if (
            name == "GIT_CONFIG_COUNT"
            or _CONFIG_ENV_RE.fullmatch(name)
            or name
            in {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_ASKPASS",
                "GIT_COMMON_DIR",
                "GIT_CONFIG",
                "GIT_DIR",
                "GIT_OBJECT_DIRECTORY",
                "GIT_PROXY_COMMAND",
                "GIT_SSH",
                "GIT_SSH_COMMAND",
                "GIT_WORK_TREE",
            }
            or name in MODEL_PROVIDER_ENV_KEYS
        ):
            env.pop(name, None)
    entries = (
        ("core.hooksPath", os.devnull),
        ("credential.interactive", "false"),
        # Ignore a repository-selected config.worktree during every host Git
        # publication command. The baseline still fingerprints that file so
        # any post-launch change fails closed before publication.
        ("extensions.worktreeConfig", "false"),
    )
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _remote_names(repo_root: Path) -> tuple[str, ...]:
    # Some callers prepare the child environment before a checkout is fully
    # materialized. The URL prefix guards still apply in that case, and there
    # is no repository-local remote list to inspect yet.
    if not (repo_root / ".git").exists():
        return ()
    try:
        completed = subprocess.run(
            ["git", "remote"],
            cwd=repo_root,
            env=_git_inspection_environment(),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return ()
    return tuple(
        line
        for raw in completed.stdout.splitlines()
        if (line := raw.strip())
        and not any(character.isspace() or ord(character) < 32 for character in line)
    )


def apply_host_owned_publication_guard(
    env: MutableMapping[str, str],
    repo_root: Path,
) -> None:
    """Prevent ordinary model-selected Git pushes without affecting commits/fetches.

    The caller must additionally remove forge/SSH credentials and enforce its
    filesystem/network sandbox. This launch-only Git layer handles embedded
    remote credentials, configured credential helpers, and the common direct
    URL forms while leaving host publication outside the child environment
    unchanged.
    """
    assert_no_agent_visible_git_credentials(repo_root)
    for name in list(env):
        if name == "GIT_CONFIG_COUNT" or _CONFIG_ENV_RE.fullmatch(name):
            env.pop(name, None)

    entries: list[tuple[str, str]] = [
        ("credential.helper", ""),
        ("credential.interactive", "false"),
        *(
            (f"url.specbutler-no-push://{label}/.pushInsteadOf", prefix)
            for label, prefix in _PUSH_PREFIXES
        ),
        # Existing relative and absolute local remotes are fenced explicitly;
        # the prefix rules above cover direct network/file URL pushes.
        *(
            (f"remote.{remote}.pushurl", f"specbutler-no-push://remote/{remote}")
            for remote in _remote_names(repo_root)
        ),
    ]
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
