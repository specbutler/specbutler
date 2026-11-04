"""Command-layer diagnostics for specs absent from the operator checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import load_repo_spec_runtime_config, resolve_specs_root

FETCH_TIMEOUT = 5


def missing_spec_guidance(repo_root: Path, spec_id: str, *, base_ref: str | None = None) -> str | None:
    """Inspect only the configured remote base; never update the checkout."""
    config = load_repo_spec_runtime_config(repo_root)
    path = resolve_specs_root(repo_root) / f"{spec_id}.md"
    if path.exists():
        return None
    try:
        relative = path.relative_to(repo_root).as_posix()
    except ValueError:
        return None
    ref = base_ref or config.base_ref
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
           "GIT_SSH_COMMAND": "ssh -oBatchMode=yes", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}

    def git(*args: str):
        return subprocess.run(
            ["git", *args], cwd=repo_root, env=env, capture_output=True,
            encoding="utf-8", errors="replace", timeout=FETCH_TIMEOUT,
        )

    stale = False
    stale_note = "Fetch failed or timed out; using the cached ref, so remote state may be stale."
    stale_miss = f"spec not found: '{spec_id}'. {stale_note}"
    try:
        # Git owns the mapping from a local branch to its upstream, including
        # upstreams whose remote branch name differs or whose cache is absent.
        local_ref = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
        if ref == "HEAD":
            local_ref = git("symbolic-ref", "--quiet", "HEAD").stdout.strip()
        local_branch = None
        if local_ref:
            tracking = git("for-each-ref", "--format=%(refname)\t%(upstream)", "--", local_ref)
            for line in tracking.stdout.splitlines():
                name, _, upstream = line.partition("\t")
                if name == local_ref:
                    if not upstream.startswith("refs/remotes/"):
                        return None
                    local_branch = local_ref.removeprefix("refs/heads/")
                    ref = upstream
                    break
        ref = ref.removeprefix("refs/remotes/")
        remotes = git("remote")
        remote = next((name for name in remotes.stdout.splitlines() if ref.startswith(name + "/")), None)
        if remote is None:
            return None
        branch = ref[len(remote) + 1:]
        try:
            fetched = git("fetch", "--no-tags", "--no-recurse-submodules", remote,
                          f"+refs/heads/{branch}:refs/remotes/{ref}")
            stale = fetched.returncode != 0
        except (OSError, subprocess.SubprocessError):
            stale = True
        if git("cat-file", "-t", f"refs/remotes/{ref}:{relative}").stdout.strip() != "blob":
            return stale_miss if stale else None
        count = git("rev-list", "--count", f"HEAD..refs/remotes/{ref}")
        behind = count.stdout.strip() if count.returncode == 0 else "an unknown number of"
        current = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        branch = local_branch or branch
        hint = "Run git pull --ff-only." if current == branch else (
            f"Switch to {branch} (git switch {branch}), then run git pull --ff-only."
        )
        message = (
            f"Spec '{spec_id}' exists on {ref} but not in this checkout. "
            f"This checkout is behind {ref} by {behind} commits. {hint}"
        )
        if stale:
            message += f" {stale_note}"
        return message
    except (OSError, subprocess.SubprocessError):
        return stale_miss if stale else None
