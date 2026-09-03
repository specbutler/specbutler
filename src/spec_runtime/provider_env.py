"""Least-privilege environments for non-interactive provider processes.

Agent processes can invoke shell commands, so every environment variable they
inherit is effectively available to repository code.  Build provider
environments from an allowlist instead of copying the operator's login shell.
Project-specific values are supplied separately by the implement setup
manifest; this module only carries process essentials and credentials required
by the selected model provider.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import uuid
from base64 import b64decode, b64encode
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from .platform_fs import FileLock, remove_tree

_PROCESS_ENV_KEYS = frozenset(
    {
        "COLORTERM",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "ALL_PROXY",
        "FORCE_COLOR",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LANGUAGE",
        "LOGNAME",
        "NO_COLOR",
        "NO_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)

PROXY_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)

# These lists are intentionally explicit.  In particular, generic names such
# as *_TOKEN, AWS_*, GOOGLE_*, and AZURE_* are not forwarded to an unrelated
# provider process.
_CLAUDE_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
    }
)

_CODEX_ENV_KEYS = frozenset(
    {
        "CODEX_API_KEY",
        "CODEX_HOME",
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT_ID",
    }
)

# Model-provider configuration is needed only by the provider parent process.
# Host-side Git publication must not inherit any of it: even with hooks and
# repository-local helpers disabled, a transport or global credential helper
# is still a separate process outside the provider trust boundary.
MODEL_PROVIDER_ENV_KEYS = frozenset({*_CLAUDE_ENV_KEYS, *_CODEX_ENV_KEYS})

_BEDROCK_ENV_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_DEFAULT_REGION",
        "AWS_DEFAULT_PROFILE",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_SDK_LOAD_CONFIG",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    }
)

_VERTEX_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GCLOUD_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    }
)

_FOUNDRY_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "AZURE_AUTHORITY_HOST",
        "AZURE_CLIENT_CERTIFICATE_PASSWORD",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_SEND_CERTIFICATE_CHAIN",
        "AZURE_FEDERATED_TOKEN_FILE",
        "AZURE_PASSWORD",
        "AZURE_TENANT_ID",
        "AZURE_USERNAME",
    }
)

# Provider parents need these values to authenticate and select their remote
# transport, but model-selected commands, hooks, and MCP subprocesses must not
# inherit them.  Keep the classification next to the provider allowlists so
# web and lifecycle launchers cannot drift as new Bedrock/Vertex/Foundry
# authentication mechanisms are added.
CLAUDE_PROVIDER_CREDENTIAL_ENV_KEYS = frozenset(
    {
        *PROXY_ENV_KEYS,
        *_CLAUDE_ENV_KEYS,
        *_BEDROCK_ENV_KEYS,
        *_VERTEX_ENV_KEYS,
        *_FOUNDRY_ENV_KEYS,
        "CLAUDE_CONFIG_DIR",
    }
)

CODEX_SECRET_ENV_KEYS = frozenset({"CODEX_API_KEY", "OPENAI_API_KEY"})

_CODEX_AUTH_MAX_BYTES = 2 * 1024 * 1024
_CODEX_RECOVERY_MAX_BYTES = 3 * 1024 * 1024


def _is_windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(
        attributes
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _read_private_regular_file(
    path: Path,
    *,
    max_bytes: int = _CODEX_AUTH_MAX_BYTES,
) -> bytes:
    """Read a bounded credential file without following its final component."""
    before = os.lstat(path)
    before_reparse = bool(
        int(getattr(before, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )
    if stat.S_ISLNK(before.st_mode) or before_reparse or not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"Codex auth path is not a regular file: {path}")
    if before.st_nlink != 1:
        raise RuntimeError(f"Codex auth path has multiple hard links: {path}")
    if before.st_size > max_bytes:
        raise RuntimeError(f"Codex auth file is unexpectedly large: {path}")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise RuntimeError(f"Codex auth file is not owned by the current user: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
    )
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError(f"Codex auth path is not a regular file: {path}")
        if file_stat.st_nlink != 1:
            raise RuntimeError(f"Codex auth path has multiple hard links: {path}")
        if (before.st_dev, before.st_ino) != (file_stat.st_dev, file_stat.st_ino):
            raise RuntimeError(f"Codex auth path changed while being opened: {path}")
        if file_stat.st_size > max_bytes:
            raise RuntimeError(f"Codex auth file is unexpectedly large: {path}")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise RuntimeError(f"Codex auth file is not owned by the current user: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise RuntimeError(f"Codex auth file is unexpectedly large: {path}")
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    after_reparse = bool(
        int(getattr(after, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )
    if (
        stat.S_ISLNK(after.st_mode)
        or after_reparse
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (file_stat.st_dev, file_stat.st_ino) != (after.st_dev, after.st_ino)
        or (hasattr(os, "getuid") and after.st_uid != os.getuid())
    ):
        raise RuntimeError(f"Codex auth path changed while being read: {path}")
    return payload


def _atomic_write_private_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace an operator credential file with private permissions."""
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=".spec-codex-auth-",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
            try:
                parent_descriptor = os.open(path.parent, os.O_RDONLY)
            except OSError:
                parent_descriptor = -1
            if parent_descriptor >= 0:
                try:
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _codex_oauth_identity(payload: bytes, *, path: Path) -> tuple[str, str]:
    """Validate copied OAuth state and return its stable account identity."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Codex OAuth state is not valid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"Codex OAuth state is not a JSON object: {path}")
    auth_mode = str(document.get("auth_mode") or "").strip().lower()
    tokens = document.get("tokens")
    if auth_mode in {"apikey", "api-key", "api_key"} or not isinstance(tokens, dict):
        raise RuntimeError(f"Codex auth state is not refreshable OAuth state: {path}")
    for name in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(name), str) or not tokens[name].strip():
            raise RuntimeError(f"Codex OAuth state is missing {name}: {path}")
    account_id = str(tokens.get("account_id") or "").strip()
    return auth_mode, account_id


def _looks_like_codex_oauth(payload: bytes) -> bool:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    auth_mode = str(document.get("auth_mode") or "").strip().lower()
    tokens = document.get("tokens")
    return (
        auth_mode not in {"apikey", "api-key", "api_key"}
        and isinstance(tokens, dict)
        and isinstance(tokens.get("access_token"), str)
        and bool(tokens["access_token"].strip())
        and isinstance(tokens.get("refresh_token"), str)
        and bool(tokens["refresh_token"].strip())
    )


def _codex_auth_lock_path(
    source_auth: Path,
    source: Mapping[str, str] | None = None,
) -> Path:
    root = _private_provider_state_subdirectory(source, "provider-locks")
    digest = sha256(str(source_auth).encode("utf-8")).hexdigest()[:32]
    return root / f"codex-auth-{digest}.lock"


def _ensure_private_provider_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = os.lstat(path)
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_windows_reparse_point(path)
        or not stat.S_ISDIR(before.st_mode)
    ):
        raise RuntimeError(f"Refusing unsafe provider-state directory: {path}")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise RuntimeError(
            f"Provider-state directory is not owned by the current user: {path}"
        )
    if os.name != "nt":
        path.chmod(0o700)
        after = os.lstat(path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError(f"Provider-state directory changed during validation: {path}")
        if stat.S_IMODE(after.st_mode) & 0o077:
            raise RuntimeError(f"Provider-state directory is not private: {path}")


def _private_provider_state_subdirectory(
    source: Mapping[str, str] | None,
    name: str,
) -> Path:
    state_root = specbutler_user_state_root(source)
    _ensure_private_provider_directory(state_root)
    child = state_root / name
    _ensure_private_provider_directory(child)
    return child


class CopiedCodexOAuthSession:
    """Own a copied OAuth credential and reconcile provider rotations safely.

    The inter-process lock is held for the complete provider lifetime. That is
    necessary because Codex refresh tokens are rotatable: two sessions copied
    from the same initial file could otherwise each consume the same refresh
    token and race while writing incompatible replacements back.
    """

    def __init__(
        self,
        *,
        source_auth: Path,
        staged_auth: Path,
        initial_payload: bytes,
        identity: tuple[str, str],
        lock: FileLock,
        recovery_root: Path,
        launch_journal: Path,
        launch_record: dict[str, object],
    ) -> None:
        self.source_auth = source_auth
        self.staged_auth = staged_auth
        self._initial_payload = initial_payload
        self._identity = identity
        self._lock = lock
        self._recovery_root = recovery_root
        self._launch_journal = launch_journal
        self._launch_record = launch_record
        self._closed = False
        self._close_lock = threading.Lock()
        self._recovery_auth: Path | None = None

    def _close(self) -> None:
        self._closed = True
        self._lock.release()

    def _retire_launch_copy(self) -> None:
        """Remove the copied credential before retiring its durable journal."""
        try:
            self.staged_auth.unlink(missing_ok=True)
        except OSError as exc:
            raise CodexOAuthReconciliationRetryableError(
                "Codex OAuth was reconciled, but its launch credential could not "
                f"be removed: {self.staged_auth}",
                recovery_path=self._launch_journal,
            ) from exc
        try:
            self._launch_journal.unlink(missing_ok=True)
        except OSError as exc:
            raise CodexOAuthReconciliationRetryableError(
                "Codex OAuth was reconciled, but its launch journal could not be "
                f"removed: {self._launch_journal}",
                recovery_path=self._launch_journal,
            ) from exc

    def _ensure_recovery_copy(self, candidate: bytes) -> Path:
        if self._recovery_auth is not None:
            return self._recovery_auth
        root = self._recovery_root
        _ensure_private_provider_directory(root)
        digest = sha256(str(self.source_auth).encode("utf-8")).hexdigest()[:16]
        recovery = root / f"codex-auth-{digest}-{uuid.uuid4().hex}.json"
        record = json.dumps(
            {
                "version": 1,
                "initial_sha256": sha256(self._initial_payload).hexdigest(),
                "candidate_base64": b64encode(candidate).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_write_private_bytes(recovery, record)
        self._recovery_auth = recovery
        return recovery

    def _mark_launch_reconciled(self, candidate: bytes) -> None:
        record = {
            **self._launch_record,
            "reconciled_sha256": sha256(candidate).hexdigest(),
        }
        payload = json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        _atomic_write_private_bytes(self._launch_journal, payload)
        self._launch_record = record

    def _retryable_error(self, message: str, candidate: bytes, exc: BaseException) -> None:
        try:
            recovery = self._ensure_recovery_copy(candidate)
        except BaseException as recovery_exc:
            raise CodexOAuthReconciliationRetryableError(
                f"{message} The launch copy remains at {self.staged_auth}; "
                f"creating a durable recovery copy also failed: {recovery_exc}",
            ) from exc
        # Once a durable, private record exists, the random launch directory
        # and in-process lock are no longer the only owners. Release the lock
        # so the next copy-backed launch can replay this record after a crash.
        try:
            self._retire_launch_copy()
        except CodexOAuthReconciliationRetryableError:
            # The inline recovery record owns the candidate bytes even if the
            # token-free launch journal or staging file could not be retired.
            # Replay is idempotent and will clean either remainder next time.
            pass
        self._close()
        raise CodexOAuthReconciliationRetryableError(
            f"{message} A private recovery copy is retained at {recovery}; "
            "the next copy-backed Codex launch will retry reconciliation.",
            recovery_path=recovery,
        ) from exc

    def finish(self) -> None:
        """Validate and atomically publish any rotated OAuth state, then unlock."""
        with self._close_lock:
            if self._closed:
                return
            try:
                candidate = _read_private_regular_file(self.staged_auth)
                candidate_identity = _codex_oauth_identity(
                    candidate,
                    path=self.staged_auth,
                )
            except Exception:
                # A corrupt, missing, or account-switched staged credential is
                # not safe to retry or publish. Release serialization so the
                # next launch can start from the unchanged canonical state.
                try:
                    self._retire_launch_copy()
                finally:
                    self._close()
                raise
            if candidate_identity != self._identity:
                try:
                    self._retire_launch_copy()
                finally:
                    self._close()
                raise RuntimeError(
                    "Codex refused to reconcile OAuth state for a different account"
                )
            try:
                current = _read_private_regular_file(self.source_auth)
            except (OSError, RuntimeError) as exc:
                self._retryable_error(
                    "Could not read canonical Codex OAuth state.",
                    candidate,
                    exc,
                )
            if current not in {self._initial_payload, candidate}:
                try:
                    self._retire_launch_copy()
                finally:
                    self._close()
                raise RuntimeError(
                    "Codex OAuth state changed outside this serialized launch; "
                    "refusing to overwrite newer operator credentials"
                )
            if candidate != current:
                try:
                    _atomic_write_private_bytes(self.source_auth, candidate)
                except OSError as exc:
                    self._retryable_error(
                        "Could not atomically publish rotated Codex OAuth state.",
                        candidate,
                        exc,
                    )
            try:
                self._mark_launch_reconciled(candidate)
            except OSError as exc:
                self._retryable_error(
                    "Codex OAuth was published, but its launch journal could not "
                    "record completion.",
                    candidate,
                    exc,
                )
            try:
                self._retire_launch_copy()
            except CodexOAuthReconciliationRetryableError as exc:
                self._retryable_error(
                    "Codex OAuth was published, but launch-state cleanup failed.",
                    candidate,
                    exc,
                )
            self._close()


class CodexOAuthReconciliationRetryableError(RuntimeError):
    """A rotated Codex OAuth copy is retained and cleanup can be retried."""

    def __init__(self, message: str, *, recovery_path: Path | None = None) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path


def _codex_source_digest(source_auth: Path) -> str:
    return sha256(str(source_auth).encode("utf-8")).hexdigest()


def _staging_parent_metadata(staged_auth: Path) -> tuple[Path, os.stat_result]:
    """Validate the exact private directory that owns one copied credential."""
    absolute_auth = Path(os.path.abspath(staged_auth))
    if absolute_auth.name != "auth.json":
        raise RuntimeError("Codex OAuth staging path must end in auth.json")
    parent = absolute_auth.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        metadata = os.lstat(parent)
    except OSError as exc:
        raise RuntimeError(
            f"Could not validate Codex OAuth staging directory: {parent}"
        ) from exc
    if os.path.normcase(str(resolved_parent)) != os.path.normcase(str(parent)):
        raise RuntimeError(
            f"Codex OAuth staging directory must not contain symlinks: {parent}"
        )
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_windows_reparse_point(parent)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise RuntimeError(f"Refusing unsafe Codex OAuth staging directory: {parent}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise RuntimeError(
            f"Codex OAuth staging directory is not owned by the current user: {parent}"
        )
    return absolute_auth, metadata


def _create_codex_oauth_launch_journal(
    source_auth: Path,
    staged_auth: Path,
    initial_payload: bytes,
    recovery_root: Path,
    *,
    remove_staging_parent_on_replay: bool,
) -> tuple[Path, Path, dict[str, object]]:
    """Journal a copy-backed launch before its credential can reach a provider.

    The journal contains only path identities and hashes. The credential bytes
    remain solely in canonical auth and the launch-scoped ``auth.json``.
    """
    absolute_auth, parent = _staging_parent_metadata(staged_auth)
    source_digest = _codex_source_digest(source_auth)
    identity = _codex_oauth_identity(initial_payload, path=source_auth)
    identity_digest = sha256(
        json.dumps(identity, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    journal = (
        recovery_root
        / f"codex-auth-{source_digest[:16]}-{uuid.uuid4().hex}.json"
    )
    record = json.dumps(
        {
            "version": 2,
            "kind": "copy-launch",
            "source_path_sha256": source_digest,
            "initial_sha256": sha256(initial_payload).hexdigest(),
            "identity_sha256": identity_digest,
            "staged_auth": str(absolute_auth),
            "staging_parent_device": int(parent.st_dev),
            "staging_parent_inode": int(parent.st_ino),
            "remove_staging_parent_on_replay": remove_staging_parent_on_replay,
            "reconciled_sha256": None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write_private_bytes(journal, record)
    return journal, absolute_auth, json.loads(record)


def _unlink_replayed_launch_copy(
    journal: Path,
    staged_auth: Path | None,
    staging_parent: Path | None,
    *,
    remove_staging_parent: bool = False,
    staging_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Retire a replayed copy and journal without following provider links."""
    if staging_parent is not None:
        _, parent_metadata = _staging_parent_metadata(staging_parent / "auth.json")
        if staging_parent_identity is not None and (
            int(parent_metadata.st_dev),
            int(parent_metadata.st_ino),
        ) != staging_parent_identity:
            raise RuntimeError(
                f"Codex OAuth staging directory changed before cleanup: {staging_parent}"
            )
    if staged_auth is not None:
        try:
            metadata = os.lstat(staged_auth)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect stale Codex OAuth copy: {staged_auth}"
            ) from exc
        else:
            if (
                stat.S_ISLNK(metadata.st_mode)
                or _is_windows_reparse_point(staged_auth)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise RuntimeError(
                    f"Refusing unsafe stale Codex OAuth copy: {staged_auth}"
                )
            staged_auth.unlink()
    if staging_parent is not None:
        if remove_staging_parent:
            # The journal marks only random, launch-scoped provider homes for
            # recursive removal. Their allowed root and exact directory inode
            # are checked again by this helper before recursive removal.
            remove_tree(staging_parent)
        else:
            for temporary in staging_parent.glob(".spec-codex-auth-*.tmp"):
                metadata = os.lstat(temporary)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or _is_windows_reparse_point(temporary)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                ):
                    raise RuntimeError(
                        f"Refusing unsafe stale Codex OAuth temporary file: {temporary}"
                    )
                temporary.unlink()
            try:
                staging_parent.rmdir()
            except OSError:
                # Stable container homes legitimately contain config files.
                # Their normal owner cleans them after the agent launch.
                pass
    journal.unlink()


def _reconcile_pending_codex_oauth_recovery(
    source_auth: Path,
    recovery_root: Path,
    source: Mapping[str, str] | None,
) -> None:
    """Replay crash-safe OAuth recovery records while the source lock is held."""
    source_digest = _codex_source_digest(source_auth)
    digest = source_digest[:16]
    if not recovery_root.exists():
        return
    recovery_paths = sorted(recovery_root.glob(f"codex-auth-{digest}-*.json"))
    recovery_versions: dict[Path, int] = {}
    recovery_payload_hashes: dict[Path, str] = {}
    inline_candidate_identities: dict[str, tuple[str, str]] = {}
    for recovery in recovery_paths:
        try:
            raw_record = _read_private_regular_file(
                recovery,
                max_bytes=_CODEX_RECOVERY_MAX_BYTES,
            )
            record = json.loads(raw_record.decode("utf-8"))
            if not isinstance(record, dict) or record.get("version") not in {1, 2}:
                raise ValueError("unsupported recovery record")
            version = int(record["version"])
            recovery_versions[recovery] = version
            recovery_payload_hashes[recovery] = sha256(raw_record).hexdigest()
            if version == 1:
                encoded_candidate = record.get("candidate_base64")
                if not isinstance(encoded_candidate, str):
                    raise ValueError("incomplete recovery record")
                candidate = b64decode(encoded_candidate, validate=True)
                inline_candidate_identities[sha256(candidate).hexdigest()] = (
                    _codex_oauth_identity(candidate, path=recovery)
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"Could not validate pending Codex OAuth recovery at {recovery}: {exc}"
            ) from exc
    # A v1 record can be the durable fallback for a v2 journal whose staging
    # file was removed before that journal itself could be retired. Process v2
    # records first, while every matching v1 candidate is still discoverable.
    recovery_paths.sort(key=lambda path: 0 if recovery_versions[path] == 2 else 1)
    for recovery in recovery_paths:
        try:
            raw_record = _read_private_regular_file(
                recovery,
                max_bytes=_CODEX_RECOVERY_MAX_BYTES,
            )
            if not secrets.compare_digest(
                sha256(raw_record).hexdigest(),
                recovery_payload_hashes[recovery],
            ):
                raise ValueError("recovery record changed during replay")
            record = json.loads(raw_record.decode("utf-8"))
            if not isinstance(record, dict) or record.get("version") not in {1, 2}:
                raise ValueError("unsupported recovery record")
            initial_sha256 = record.get("initial_sha256")
            if not isinstance(initial_sha256, str):
                raise ValueError("incomplete recovery record")
            current = _read_private_regular_file(source_auth)
            current_identity = _codex_oauth_identity(current, path=source_auth)
            staged_auth: Path | None = None
            staging_parent: Path | None = None
            remove_staging_parent = False
            if record.get("version") == 1:
                encoded_candidate = record.get("candidate_base64")
                if not isinstance(encoded_candidate, str):
                    raise ValueError("incomplete recovery record")
                candidate = b64decode(encoded_candidate, validate=True)
                candidate_identity = _codex_oauth_identity(candidate, path=recovery)
            else:
                if (
                    record.get("kind") != "copy-launch"
                    or record.get("source_path_sha256") != source_digest
                    or not isinstance(record.get("identity_sha256"), str)
                    or not isinstance(record.get("staged_auth"), str)
                    or not isinstance(record.get("staging_parent_device"), int)
                    or isinstance(record.get("staging_parent_device"), bool)
                    or not isinstance(record.get("staging_parent_inode"), int)
                    or isinstance(record.get("staging_parent_inode"), bool)
                    or not isinstance(
                        record.get("remove_staging_parent_on_replay"),
                        bool,
                    )
                    or (
                        record.get("reconciled_sha256") is not None
                        and not isinstance(record.get("reconciled_sha256"), str)
                    )
                ):
                    raise ValueError("invalid Codex OAuth launch journal")
                staged_value = record["staged_auth"]
                staged_auth = Path(staged_value)
                if not staged_auth.is_absolute() or staged_auth.name != "auth.json":
                    raise ValueError("invalid Codex OAuth staging path")
                staging_parent = staged_auth.parent
                remove_staging_parent = record["remove_staging_parent_on_replay"]
                reconciled_sha256 = record.get("reconciled_sha256")
                if isinstance(reconciled_sha256, str) and (
                    len(reconciled_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in reconciled_sha256)
                ):
                    raise ValueError("invalid reconciled Codex OAuth digest")
                if remove_staging_parent:
                    provider_homes_root = Path(
                        os.path.abspath(_codex_provider_homes_root(source))
                    )
                    if (
                        staging_parent.parent != provider_homes_root
                        or not staging_parent.name.startswith("spec-codex-home-")
                    ):
                        raise ValueError(
                            "Codex OAuth launch journal has an invalid ephemeral home"
                        )
                current_identity_digest = sha256(
                    json.dumps(current_identity, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if not secrets.compare_digest(
                    current_identity_digest,
                    record["identity_sha256"],
                ):
                    raise ValueError(
                        "Codex OAuth launch journal belongs to a different account"
                    )
                try:
                    checked_auth, parent_metadata = _staging_parent_metadata(staged_auth)
                except RuntimeError:
                    # A missing launch directory is safe to retire only when the
                    # canonical credential still matches the pre-launch state.
                    current_sha256 = sha256(current).hexdigest()
                    if not staging_parent.exists() and current_sha256 in {
                        initial_sha256,
                        reconciled_sha256,
                    } | {
                        digest
                        for digest, identity in inline_candidate_identities.items()
                        if identity == current_identity
                    }:
                        _unlink_replayed_launch_copy(recovery, None, None)
                        continue
                    raise
                if (
                    checked_auth != staged_auth
                    or int(parent_metadata.st_dev) != record["staging_parent_device"]
                    or int(parent_metadata.st_ino) != record["staging_parent_inode"]
                ):
                    raise ValueError("Codex OAuth staging directory identity changed")
                try:
                    candidate = _read_private_regular_file(staged_auth)
                except FileNotFoundError:
                    # finish() removes the staged token before the journal. A
                    # crash in that narrow interval is complete only when the
                    # journal confirms the exact canonical payload digest.
                    if sha256(current).hexdigest() not in {
                        initial_sha256,
                        reconciled_sha256,
                    } | {
                        digest
                        for digest, identity in inline_candidate_identities.items()
                        if identity == current_identity
                    }:
                        raise ValueError(
                            "Codex OAuth staging copy disappeared before reconciliation"
                        )
                    _unlink_replayed_launch_copy(
                        recovery,
                        None,
                        staging_parent,
                        remove_staging_parent=remove_staging_parent,
                        staging_parent_identity=(
                            int(record["staging_parent_device"]),
                            int(record["staging_parent_inode"]),
                        ),
                    )
                    continue
                candidate_identity = _codex_oauth_identity(candidate, path=staged_auth)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"Could not validate pending Codex OAuth recovery at {recovery}: {exc}"
            ) from exc
        if candidate_identity != current_identity:
            raise RuntimeError(
                "Pending Codex OAuth recovery belongs to a different account; "
                f"inspect the private recovery record at {recovery}"
            )
        if current != candidate:
            if sha256(current).hexdigest() != initial_sha256:
                raise RuntimeError(
                    "Canonical Codex OAuth state changed after a failed reconciliation; "
                    f"inspect the private recovery record at {recovery}"
                )
            try:
                _atomic_write_private_bytes(source_auth, candidate)
            except OSError as exc:
                raise CodexOAuthReconciliationRetryableError(
                    "Could not replay pending Codex OAuth recovery; the private "
                    f"record remains at {recovery}",
                    recovery_path=recovery,
                ) from exc
        _unlink_replayed_launch_copy(
            recovery,
            staged_auth,
            staging_parent,
            remove_staging_parent=remove_staging_parent,
            staging_parent_identity=(
                int(record["staging_parent_device"]),
                int(record["staging_parent_inode"]),
            )
            if staging_parent is not None
            else None,
        )


def copy_codex_auth_for_launch(
    source_auth: Path,
    staged_auth: Path,
    *,
    source: Mapping[str, str] | None = None,
    remove_staging_parent_on_replay: bool = False,
) -> CopiedCodexOAuthSession | None:
    """Copy Codex auth for one launch and lock refreshable OAuth sessions.

    API-key and unrecognized legacy files retain their prior copy-only
    behavior. Only validated OAuth files are reconciled back to the operator's
    canonical auth file.
    """
    try:
        canonical_source = source_auth.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"Could not resolve Codex auth file {source_auth}: {exc}") from exc
    # Never block an orchestrator worker or the async web event loop for the
    # lifetime of another model session. Copy-backed OAuth is single-writer:
    # concurrent launches fail promptly and can be retried after the active
    # provider exits.
    lock = FileLock(_codex_auth_lock_path(canonical_source, source), blocking=False)
    if not lock.acquire():
        raise RuntimeError(
            "Another copy-backed Codex OAuth session is already active; "
            "wait for it to finish before starting this launch"
        )
    try:
        recovery_root = _private_provider_state_subdirectory(source, "oauth-recovery")
        _reconcile_pending_codex_oauth_recovery(
            canonical_source,
            recovery_root,
            source,
        )
        payload = _read_private_regular_file(canonical_source)
        oauth = _looks_like_codex_oauth(payload)
        if staged_auth.is_symlink() or _is_windows_reparse_point(staged_auth):
            staged_auth.unlink()
        if not oauth:
            _atomic_write_private_bytes(staged_auth, payload)
            lock.release()
            return None
        identity = _codex_oauth_identity(payload, path=canonical_source)
        (
            launch_journal,
            absolute_staged_auth,
            launch_record,
        ) = _create_codex_oauth_launch_journal(
            canonical_source,
            staged_auth,
            payload,
            recovery_root,
            remove_staging_parent_on_replay=remove_staging_parent_on_replay,
        )
        try:
            _atomic_write_private_bytes(absolute_staged_auth, payload)
        except BaseException as exc:
            try:
                _unlink_replayed_launch_copy(
                    launch_journal,
                    absolute_staged_auth,
                    absolute_staged_auth.parent,
                    remove_staging_parent=False,
                )
            except (OSError, RuntimeError) as cleanup_exc:
                raise RuntimeError(
                    "Could not securely clean a failed Codex OAuth launch copy; "
                    f"the token-free recovery journal remains at {launch_journal}: "
                    f"{cleanup_exc}"
                ) from exc
            raise
        return CopiedCodexOAuthSession(
            source_auth=canonical_source,
            staged_auth=absolute_staged_auth,
            initial_payload=payload,
            identity=identity,
            lock=lock,
            recovery_root=recovery_root,
            launch_journal=launch_journal,
            launch_record=launch_record,
        )
    except BaseException:
        lock.release()
        raise


class EphemeralCodexHome:
    """Temporary CODEX_HOME owner that reconciles copied OAuth before removal."""

    def __init__(
        self,
        temporary: tempfile.TemporaryDirectory[str],
        auth_session: CopiedCodexOAuthSession | None,
        home_lease: FileLock,
        gc_lock_path: Path,
    ) -> None:
        self._temporary = temporary
        self._auth_session = auth_session
        self._home_lease = home_lease
        self._gc_lock_path = gc_lock_path
        self._closed = False
        self._lock = threading.Lock()

    def _cleanup_temporary_home(self) -> None:
        gc_lock = FileLock(self._gc_lock_path)
        if not gc_lock.acquire():
            raise RuntimeError(
                f"Could not acquire Codex provider-home cleanup lock: {self._gc_lock_path}"
            )
        try:
            # Windows cannot remove an open locked file. Serialize the narrow
            # unlock/delete interval so a concurrent scanner cannot race this
            # owner's normal TemporaryDirectory cleanup.
            self._home_lease.release()
            self._temporary.cleanup()
        finally:
            gc_lock.release()

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._auth_session is not None:
                    self._auth_session.finish()
            except CodexOAuthReconciliationRetryableError as exc:
                if exc.recovery_path is not None:
                    self._cleanup_temporary_home()
                    self._closed = True
                raise
            except Exception:
                # Validation failures are terminal and the canonical auth was
                # not overwritten, so remove the rejected launch copy.
                self._cleanup_temporary_home()
                self._closed = True
                raise
            self._cleanup_temporary_home()
            self._closed = True

    def __enter__(self) -> str:
        return self._temporary.name

    def __exit__(self, *_: object) -> None:
        self.cleanup()


def _codex_provider_homes_root(source: Mapping[str, str] | None) -> Path:
    values = os.environ if source is None else source
    if sys.platform == "win32":
        local_app_data = values.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data).expanduser() / "SpecButler" / "provider-homes"
        return Path.home() / "AppData" / "Local" / "SpecButler" / "provider-homes"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "SpecButler"
            / "provider-homes"
        )
    return Path.home() / ".local" / "state" / "specbutler" / "provider-homes"


_CODEX_EPHEMERAL_HOME_LEASE = ".specbutler-home.lease"
_CODEX_EPHEMERAL_HOME_GC_LOCK = ".specbutler-home-gc.lock"


def _validate_ephemeral_codex_home(path: Path, parent: Path) -> os.stat_result:
    absolute = Path(os.path.abspath(path))
    expected_parent = Path(os.path.abspath(parent))
    if (
        absolute.parent != expected_parent
        or not absolute.name.startswith("spec-codex-home-")
    ):
        raise RuntimeError(f"Refusing invalid ephemeral Codex home: {path}")
    metadata = os.lstat(absolute)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_windows_reparse_point(absolute)
        or not stat.S_ISDIR(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise RuntimeError(f"Refusing unsafe ephemeral Codex home: {path}")
    return metadata


def _journaled_codex_staging_parents(
    source: Mapping[str, str] | None,
) -> set[Path]:
    recovery_root = _private_provider_state_subdirectory(source, "oauth-recovery")
    protected: set[Path] = set()
    for journal in recovery_root.glob("codex-auth-*.json"):
        try:
            record = json.loads(
                _read_private_regular_file(
                    journal,
                    max_bytes=_CODEX_RECOVERY_MAX_BYTES,
                ).decode("utf-8")
            )
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not inspect Codex OAuth recovery before home cleanup: {journal}"
            ) from exc
        if not isinstance(record, dict) or record.get("version") not in {1, 2}:
            raise RuntimeError(f"Invalid Codex OAuth recovery record: {journal}")
        if record.get("version") == 1:
            continue
        staged_value = record.get("staged_auth")
        if not isinstance(staged_value, str):
            raise RuntimeError(f"Invalid Codex OAuth launch journal: {journal}")
        staged_auth = Path(staged_value)
        if not staged_auth.is_absolute() or staged_auth.name != "auth.json":
            raise RuntimeError(f"Invalid Codex OAuth launch journal: {journal}")
        protected.add(Path(os.path.abspath(staged_auth.parent)))
    return protected


def _reconcile_stale_ephemeral_codex_homes(
    parent: Path,
    *,
    protected: set[Path],
) -> None:
    """Remove dead launch homes without racing another live API-key session."""
    for home in sorted(parent.glob("spec-codex-home-*")):
        _validate_ephemeral_codex_home(home, parent)
        if Path(os.path.abspath(home)) in protected:
            continue
        lease_path = home / _CODEX_EPHEMERAL_HOME_LEASE
        try:
            before = os.lstat(lease_path)
        except FileNotFoundError:
            # Old installations and a creator interrupted before acquiring its
            # lease are unowned but cannot be distinguished from a live legacy
            # process. They contain no newly copied credential before the lease.
            continue
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_windows_reparse_point(lease_path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        ):
            raise RuntimeError(f"Refusing unsafe ephemeral Codex home lease: {lease_path}")
        lease = FileLock(lease_path, blocking=False)
        if not lease.acquire():
            continue
        try:
            opened = os.fstat(lease.file.fileno()) if lease.file is not None else None
            if opened is None or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise RuntimeError(
                    f"Ephemeral Codex home lease changed while being opened: {lease_path}"
                )
        finally:
            lease.release()
        _validate_ephemeral_codex_home(home, parent)
        remove_tree(home)


# Repository-defined setup output is data for the implementation tools; it is
# not allowed to reconfigure or instrument the provider process that owns the
# operator's model credential.  Reserve process-discovery/profile/TLS values,
# provider namespaces, and common runtime-loader injection controls.  Project
# variables such as DATABASE_URL, DB_PASSWORD, and STRIPE_API_KEY remain valid
# and are transported separately as explicitly declared values.
_SETUP_PROVIDER_PROCESS_CONTROL_KEYS = frozenset(
    {
        *_PROCESS_ENV_KEYS,
        *PROXY_ENV_KEYS,
        "BASHOPTS",
        "BASH_ENV",
        "CDPATH",
        "CLASSPATH",
        "CORECLR_ENABLE_PROFILING",
        "CORECLR_PROFILER",
        "CORECLR_PROFILER_PATH",
        "COR_ENABLE_PROFILING",
        "COR_PROFILER",
        "COR_PROFILER_PATH",
        "DOTNET_STARTUP_HOOKS",
        "ENV",
        "GCONV_PATH",
        "GI_TYPELIB_PATH",
        "GTK_PATH",
        "IFS",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "LUA_CPATH",
        "LUA_PATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "OPENSSL_CONF",
        "PERL5LIB",
        "PERL5OPT",
        "PROMPT_COMMAND",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "RUBYLIB",
        "RUBYOPT",
        "SHELLOPTS",
        "SSLKEYLOGFILE",
        "TCLLIBPATH",
        "ZDOTDIR",
        "_JAVA_OPTIONS",
    }
)


def is_provider_process_startup_control_env_name(name: str) -> bool:
    """Return whether *name* can alter provider startup or runtime loading."""
    upper_name = name.upper()
    security_sensitive_process_keys = {
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
    return (
        upper_name in (_SETUP_PROVIDER_PROCESS_CONTROL_KEYS - _PROCESS_ENV_KEYS)
        or upper_name in PROXY_ENV_KEYS
        or upper_name in security_sensitive_process_keys
        or upper_name.startswith(("LD_", "DYLD_"))
    )


def sanitize_implement_setup_environment(
    provider: str,
    values: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Separate project values from provider/startup control variables.

    Setup hooks execute before the provider and may be repository-controlled.
    Passing their output verbatim would let a checkout replace ``PATH`` with a
    fake provider executable, redirect authenticated API traffic, or inject a
    runtime preload before the model-tool sandbox exists.  Return the admitted
    project values and the blocked names so callers can surface the decision
    without ever logging their values.
    """
    normalized_provider = provider.strip().lower()
    blocked: list[str] = []
    admitted: dict[str, str] = {}
    for raw_name, value in values.items():
        name = str(raw_name)
        upper_name = name.upper()
        provider_control = (
            normalized_provider == "claude"
            and (
                upper_name.startswith(("ANTHROPIC_", "CLAUDE_"))
                or upper_name in _BEDROCK_ENV_KEYS
                or upper_name in _VERTEX_ENV_KEYS
                or upper_name in _FOUNDRY_ENV_KEYS
            )
        ) or (
            normalized_provider == "codex"
            and upper_name.startswith(("CODEX_", "OPENAI_"))
        )
        if (
            upper_name.startswith("SPEC_")
            or upper_name.startswith("GIT_CONFIG_")
            or upper_name
            in {
                "GIT_ASKPASS",
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_SSH",
                "GIT_SSH_COMMAND",
                "GIT_TERMINAL_PROMPT",
            }
            or upper_name in _SETUP_PROVIDER_PROCESS_CONTROL_KEYS
            or upper_name.startswith(("LD_", "DYLD_"))
            or provider_control
        ):
            blocked.append(name)
            continue
        admitted[name] = value
    return admitted, tuple(sorted(blocked))


def create_ephemeral_codex_home(
    source: Mapping[str, str] | None = None,
    *,
    copy_auth: bool | None = None,
) -> tuple[EphemeralCodexHome, Path]:
    """Create a private Codex home whose auth does not require secret env.

    The caller owns the returned temporary-directory context and must keep it
    alive for the provider process lifetime. OAuth auth is linked on POSIX so
    refreshes reach the operator's canonical file; native Windows uses a copy.
    API-key-only setups are converted to Codex's documented local auth-file
    representation. This keeps provider secrets out of process ancestry that
    model-controlled Linux commands can inspect through procfs.
    """
    values = os.environ if source is None else source
    source_home = Path(
        values.get("CODEX_HOME") or Path.home() / ".codex"
    ).expanduser()
    # Codex deliberately refuses to install its command-runner PATH helpers
    # below a system temporary directory. Keep launch-scoped homes in
    # Spec Butler's private operator-state area instead: it remains outside
    # the repository/model write roots, yet supports Codex's native runner.
    # Do not honor a system-temporary XDG_STATE_HOME here. Codex refuses to
    # create the helper executables used by its shell/edit surface beneath a
    # temporary directory. This root is provider runtime state, not the
    # configurable web-token location.
    parent = _codex_provider_homes_root(values)
    _ensure_private_provider_directory(parent)
    gc_lock_path = parent / _CODEX_EPHEMERAL_HOME_GC_LOCK
    gc_lock = FileLock(gc_lock_path)
    if not gc_lock.acquire():
        raise RuntimeError(
            f"Could not acquire Codex provider-home creation lock: {gc_lock_path}"
        )
    try:
        _reconcile_stale_ephemeral_codex_homes(
            parent,
            protected=_journaled_codex_staging_parents(values),
        )
        context = tempfile.TemporaryDirectory(
            prefix="spec-codex-home-",
            dir=parent,
        )
        home = Path(context.name)
        home_lease = FileLock(home / _CODEX_EPHEMERAL_HOME_LEASE)
        if not home_lease.acquire():
            context.cleanup()
            raise RuntimeError(f"Could not acquire ephemeral Codex home lease: {home}")
        if os.name != "nt" and home_lease.path.exists():
            home_lease.path.chmod(0o600)
    finally:
        gc_lock.release()
    source_auth = source_home / "auth.json"
    destination_auth = home / "auth.json"
    auth_session: CopiedCodexOAuthSession | None = None
    try:
        api_key = values.get("OPENAI_API_KEY") or values.get("CODEX_API_KEY")
        if api_key:
            # An explicitly exported API key is an operator choice and, unlike
            # rotatable OAuth state, supports independent concurrent homes.
            _atomic_write_private_bytes(
                destination_auth,
                json.dumps(
                    {"auth_mode": "apikey", "OPENAI_API_KEY": api_key}
                ).encode("utf-8"),
            )
        elif source_auth.is_file():
            effective_copy_auth = (
                sys.platform == "win32" if copy_auth is None else copy_auth
            )
            if effective_copy_auth:
                auth_session = copy_codex_auth_for_launch(
                    source_auth,
                    destination_auth,
                    source=values,
                    remove_staging_parent_on_replay=True,
                )
            else:
                destination_auth.symlink_to(source_auth.resolve())
    except BaseException:
        cleanup_lock = FileLock(gc_lock_path)
        if cleanup_lock.acquire():
            try:
                home_lease.release()
                context.cleanup()
            finally:
                cleanup_lock.release()
        else:
            home_lease.release()
        raise
    return EphemeralCodexHome(
        context,
        auth_session,
        home_lease,
        gc_lock_path,
    ), home


def specbutler_user_state_root(
    source: Mapping[str, str] | None = None,
) -> Path:
    """Return Spec Butler's machine-local operator-state directory.

    Keeping this resolver outside the web package lets provider launchers deny
    model-controlled reads of web credentials without importing the web stack
    or duplicating platform-specific paths.
    """
    values = os.environ if source is None else source
    if os.name == "nt":
        configured = values.get("LOCALAPPDATA")
        if configured:
            return Path(configured).expanduser() / "SpecButler"
        return Path.home() / "AppData" / "Local" / "SpecButler"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SpecButler"
    configured = values.get("XDG_STATE_HOME")
    if configured and Path(configured).expanduser().is_absolute():
        return Path(configured).expanduser() / "specbutler"
    return Path.home() / ".local" / "state" / "specbutler"


def protected_operator_paths(
    source: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return host credential/state paths that agent tools must not read.

    Provider processes still need to authenticate *themselves*, but commands
    chosen by a model do not need the operator's credential stores.  Keep this
    list deliberately broader than the selected provider: a Codex shell must
    not be able to trade an OpenAI credential for an SSH, forge, cloud, or
    Claude credential.  Callers may add a session-specific provider home.

    This is a targeted denylist rather than a blanket home-directory deny.
    Developer tools and project dependencies are commonly installed below the
    home directory (for example npm/nvm and Python virtual environments), and
    denying the whole directory would make otherwise safe project commands
    unusable.
    """
    values = os.environ if source is None else source
    home = Path.home()
    paths = {
        specbutler_user_state_root(values),
        home / ".aws",
        home / ".azure",
        home / ".claude",
        home / ".claude.json",
        home / ".codex",
        home / ".config" / "gh",
        home / ".config" / "gcloud",
        home / ".docker" / "config.json",
        home / ".gitconfig",
        home / ".git-credentials",
        home / ".config" / "git",
        home / ".kube",
        home / ".netrc",
        home / ".npmrc",
        home / ".pypirc",
        home / ".ssh",
    }
    for home_key in ("HOME", "USERPROFILE"):
        configured_home = str(values.get(home_key, "")).strip()
        if configured_home:
            candidate_home = Path(configured_home).expanduser()
            paths.update(
                {
                    candidate_home / ".gitconfig",
                    candidate_home / ".config" / "git",
                }
            )
    configured_xdg = str(values.get("XDG_CONFIG_HOME", "")).strip()
    if configured_xdg:
        paths.add(Path(configured_xdg).expanduser() / "git")
    if os.name == "nt":
        app_data = values.get("APPDATA")
        if app_data:
            app_data_path = Path(app_data).expanduser()
            paths.update(
                {
                    app_data_path / "gh",
                    app_data_path / "GitHub CLI",
                }
            )
    elif sys.platform.startswith("linux"):
        # Keep procfs denied where the provider sandbox honors ordinary path
        # rules. Some sandbox implementations remount a minimal procfs after
        # applying path rules, so launchers must also remove provider secrets
        # from the parent environment rather than relying on this alone.
        paths.add(Path("/proc"))

    for key in (
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_FEDERATED_TOKEN_FILE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "DOCKER_CONFIG",
        "GH_CONFIG_DIR",
        "GIT_CONFIG_GLOBAL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
        "NETRC",
        "NPM_CONFIG_USERCONFIG",
        "PIP_CONFIG_FILE",
        "SSH_AUTH_SOCK",
    ):
        configured = str(values.get(key, "")).strip()
        if configured:
            # KUBECONFIG may contain a platform-separated search path.
            for value in configured.split(os.pathsep):
                if value:
                    paths.add(Path(value).expanduser())

    # Bubblewrap implements denies with mount points. Redundant child entries
    # below an already-denied directory are not just noise: creating the child
    # mount after making its parent read-only can make sandbox startup fail.
    # Resolve symlinks and keep only the shallowest covering paths.
    resolved = sorted(
        {path.resolve(strict=False) for path in paths},
        key=lambda path: (len(path.parts), str(path)),
    )
    minimal: list[Path] = []
    for candidate in resolved:
        if any(
            candidate == parent or candidate.is_relative_to(parent)
            for parent in minimal
        ):
            continue
        minimal.append(candidate)
    return tuple(minimal)


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def minimal_provider_environment(
    provider: str,
    source: Mapping[str, str] | None = None,
    *,
    include_provider_auth: bool = True,
    extra_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Return a minimal environment for a non-interactive provider process.

    ``extra_keys`` is the explicit escape hatch for a caller-owned integration
    (for example, the name of an MCP bearer-token variable).  Values are never
    selected by fuzzy secret-name matching.
    """

    values = os.environ if source is None else source
    provider_keys: frozenset[str]
    normalized = provider.strip().lower()
    if normalized == "claude":
        provider_keys = _CLAUDE_ENV_KEYS
    elif normalized == "codex":
        provider_keys = _CODEX_ENV_KEYS
    else:
        provider_keys = frozenset()

    allowed = set(_PROCESS_ENV_KEYS)
    allowed.update(extra_keys)
    if include_provider_auth:
        allowed.update(provider_keys)
        if normalized == "claude":
            # Cloud-provider credentials are forwarded only when the matching
            # Claude transport is explicitly selected. An ambient AWS login,
            # for example, must not leak into a direct Anthropic session.
            if _enabled(values.get("CLAUDE_CODE_USE_BEDROCK")):
                allowed.update(_BEDROCK_ENV_KEYS)
            if _enabled(values.get("CLAUDE_CODE_USE_VERTEX")):
                allowed.update(_VERTEX_ENV_KEYS)
            if _enabled(values.get("CLAUDE_CODE_USE_FOUNDRY")):
                allowed.update(_FOUNDRY_ENV_KEYS)

    result = {key: value for key, value in values.items() if key in allowed}
    # Locale category overrides are process settings, not credentials, and
    # Python/subprocess tooling often depends on them.
    result.update(
        {
            key: value
            for key, value in values.items()
            if key.startswith("LC_")
        }
    )
    return result


def provider_environment_overlay(
    provider: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an SDK overlay that neutralizes every non-allowlisted value.

    Some provider SDKs merge their ``env`` option on top of ``os.environ``
    instead of replacing the child environment. Supplying only allowed keys
    would therefore preserve all ambient secrets. Empty-string entries remove
    their values from the effective child environment while retaining the
    explicitly selected provider/process values.
    """

    values = os.environ if source is None else source
    allowed = minimal_provider_environment(provider, values)
    overlay = {key: "" for key in values if key not in allowed}
    overlay.update(allowed)
    return overlay
