"""Token generation, cookie/bearer authentication middleware."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_LENGTH = 32  # 32 bytes = 256 bits of entropy
COOKIE_NAME_PREFIX = "spec_session"


def cookie_name_for_port(port: int | None) -> str:
    """Return the per-instance cookie name.

    Browsers do not scope cookies by port, so two ``spec web`` instances on
    the same host but different ports would otherwise overwrite each other's
    session cookie. Suffixing the port keeps both jars distinct.
    """
    return f"{COOKIE_NAME_PREFIX}_{port}" if port else COOKIE_NAME_PREFIX


def _web_state_dir(repo_root: Path) -> Path:
    from spec_runtime.config import load_spec_runtime_config

    config = load_spec_runtime_config(require=False)
    return repo_root / config.paths.state_dir / "web"


def _token_path(repo_root: Path) -> Path:
    return _web_state_dir(repo_root) / "auth-token"


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_LENGTH)


def load_or_create_token(repo_root: Path) -> str:
    path = _token_path(repo_root)
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = generate_token()
    path.write_text(token)
    os.chmod(path, 0o600)
    return token


def reset_token(repo_root: Path) -> str:
    path = _token_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = generate_token()
    path.write_text(token)
    os.chmod(path, 0o600)
    return token


def read_token(repo_root: Path) -> str | None:
    path = _token_path(repo_root)
    if path.exists():
        token = path.read_text().strip()
        return token if token else None
    return None


def extract_token_from_request(
    scope: dict,
    headers: dict[str, str],
    cookies: dict[str, str],
    cookie_name: str = COOKIE_NAME_PREFIX,
) -> str | None:
    qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
    for pair in qs.split("&"):
        if "=" in pair:
            key, _, value = pair.partition("=")
            if key == "token":
                return value

    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    return cookies.get(cookie_name)


def parse_cookies(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies
