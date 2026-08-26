from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.message import Message
from unittest.mock import patch

from spec_runtime.config import SpecPathConfig, SpecRuntimeConfig, UpdateConfig, load_spec_runtime_config
from spec_runtime.update import (
    InstallInfo,
    UpdateCacheEntry,
    _check_template_drift,
    _git_requirement_url,
    _upgrade_command_for_latest_release,
    cache_is_fresh,
    cmd_update,
    detect_installation,
    fetch_latest_version,
    maybe_print_update_notice,
    read_update_cache,
    refresh_update_cache,
    resolve_repo_slug,
    update_cache_path,
    write_update_cache,
)


class FakeDistribution:
    def __init__(self, files: dict[str, str | None], metadata_values: dict[str, str | list[str]] | None = None):
        self._files = files
        self.metadata = Message()
        for key, value in (metadata_values or {}).items():
            if isinstance(value, list):
                for entry in value:
                    self.metadata[key] = entry
            else:
                self.metadata[key] = value

    def read_text(self, filename: str) -> str | None:
        return self._files.get(filename)


def _config(*, enabled: bool = True) -> SpecRuntimeConfig:
    return SpecRuntimeConfig(
        paths=SpecPathConfig(state_dir=".spec-state"),
        update=UpdateConfig(check_enabled=enabled),
    )


def test_detect_installation_builds_git_upgrade_command():
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "ssh://git@github.com/acme/spec.git",
                    "vcs_info": {"vcs": "git", "requested_revision": "main"},
                }
            ),
            "INSTALLER": "pip\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "git"
    assert info.upgrade_command == (
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "specbutler @ git+ssh://git@github.com/acme/spec.git@main",
    )


def test_git_requirement_url_includes_subdirectory():
    direct_url = {
        "url": "https://github.com/acme/monorepo.git",
        "vcs_info": {"vcs": "git", "requested_revision": "main"},
        "subdirectory": "packages/spec",
    }
    assert (
        _git_requirement_url(direct_url) == "git+https://github.com/acme/monorepo.git@main#subdirectory=packages/spec"
    )


def test_git_requirement_url_omits_subdirectory_when_absent():
    direct_url = {
        "url": "https://github.com/acme/spec.git",
        "vcs_info": {"vcs": "git", "requested_revision": "main"},
    }
    assert _git_requirement_url(direct_url) == "git+https://github.com/acme/spec.git@main"


def test_tagged_pipx_install_advances_to_latest_release_tag():
    info = InstallInfo(
        method="pipx",
        current_version="0.2.3",
        upgrade_command=("pipx", "upgrade", "specbutler"),
        direct_url={
            "url": "https://github.com/acme/spec.git",
            "vcs_info": {"vcs": "git", "requested_revision": "v0.2.3"},
        },
        installer="pipx",
    )

    assert _upgrade_command_for_latest_release(info, "0.2.4") == (
        "pipx",
        "runpip",
        "specbutler",
        "install",
        "--upgrade",
        "specbutler @ git+https://github.com/acme/spec.git@v0.2.4",
    )


def test_branch_vcs_install_stays_on_original_channel():
    info = InstallInfo(
        method="git",
        current_version="0.2.3",
        upgrade_command=("python", "-m", "pip", "install", "git+example@main"),
        direct_url={
            "url": "https://github.com/acme/spec.git",
            "vcs_info": {"vcs": "git", "requested_revision": "main"},
        },
        installer="pip",
    )

    assert _upgrade_command_for_latest_release(info, "0.2.4") == info.upgrade_command


def test_detect_installation_preserves_subdirectory_in_upgrade_command():
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "ssh://git@github.com/acme/monorepo.git",
                    "vcs_info": {"vcs": "git", "requested_revision": "main"},
                    "subdirectory": "packages/spec",
                }
            ),
            "INSTALLER": "pip\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "git"
    assert info.upgrade_command == (
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "specbutler @ git+ssh://git@github.com/acme/monorepo.git@main#subdirectory=packages/spec",
    )


def test_detect_installation_preserves_credentials_in_git_upgrade_command():
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "https://oauth2:secret-token@github.com/acme/spec.git",
                    "vcs_info": {"vcs": "git", "requested_revision": "main"},
                }
            ),
            "INSTALLER": "pip\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "git"
    # Credentials are kept in the command so pip can authenticate to private repos.
    assert info.upgrade_command == (
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "specbutler @ git+https://oauth2:secret-token@github.com/acme/spec.git@main",
    )
    # Display redaction strips them for user-facing output.
    from spec_runtime.update import _display_command

    displayed = _display_command(info.upgrade_command)
    assert "secret-token" not in displayed
    assert "oauth2" not in displayed


def test_detect_installation_recognizes_editable_installs():
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps({"url": "file:///repo", "dir_info": {"editable": True}}),
            "INSTALLER": "pip\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "editable"
    assert info.message == "Editable install — pull and reinstall manually"


def test_unknown_install_manual_guidance_uses_runtime_repository() -> None:
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps({"url": "https://downloads.example.invalid/spec.whl"}),
            "INSTALLER": "pip\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
        patch(
            "spec_runtime.update.runtime_repository_https_url",
            return_value="https://github.com/acme/spec.git",
        ),
    ):
        info = detect_installation()

    assert info.method == "unknown"
    assert 'specbutler @ git+https://github.com/acme/spec.git' in info.message


def test_detect_installation_uses_pipx_upgrade_when_installer_matches():
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "ssh://git@github.com/acme/spec.git",
                    "vcs_info": {"vcs": "git"},
                }
            ),
            "INSTALLER": "pipx\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "pipx"
    assert info.upgrade_command == ("pipx", "upgrade", "specbutler")


def test_detect_installation_recognizes_real_pipx_metadata(tmp_path):
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "https://github.com/acme/spec.git",
                    "vcs_info": {"vcs": "git", "requested_revision": "v0.3.0"},
                }
            ),
            # pipx delegates package installation to pip, so this is the value
            # found in a real pipx-managed distribution.
            "INSTALLER": "pip\n",
        }
    )
    (tmp_path / "pipx_metadata.json").write_text(
        json.dumps(
            {
                "pipx_metadata_version": "0.3",
                "main_package": {"package": "specbutler", "package_version": "0.3.0"},
            }
        )
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.3.0"),
        patch("spec_runtime.update.sys.prefix", str(tmp_path)),
    ):
        info = detect_installation()

    assert info.method == "pipx"
    assert info.upgrade_command == ("pipx", "upgrade", "specbutler")


def test_detect_installation_ignores_other_pipx_package_metadata(tmp_path):
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "https://github.com/acme/spec.git",
                    "vcs_info": {"vcs": "git", "requested_revision": "v0.3.0"},
                }
            ),
            "INSTALLER": "pip\n",
        }
    )
    (tmp_path / "pipx_metadata.json").write_text(
        json.dumps({"pipx_metadata_version": "0.3", "main_package": {"package": "another-tool"}})
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.3.0"),
        patch("spec_runtime.update.sys.prefix", str(tmp_path)),
    ):
        info = detect_installation()

    assert info.method == "git"


def test_detect_installation_preserves_vcs_url_for_uv_managed_git_installs():
    dist = FakeDistribution(
        {
            "direct_url.json": json.dumps(
                {
                    "url": "ssh://git@github.com/acme/spec.git",
                    "vcs_info": {"vcs": "git", "requested_revision": "main"},
                }
            ),
            "INSTALLER": "uv\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "git"
    assert info.upgrade_command == (
        "uv",
        "pip",
        "install",
        "--upgrade",
        "specbutler @ git+ssh://git@github.com/acme/spec.git@main",
    )


def test_detect_installation_uses_uv_upgrade_when_installer_matches_without_vcs_url():
    dist = FakeDistribution(
        {
            "INSTALLER": "uv\n",
        }
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.method == "uv"
    assert info.upgrade_command == ("uv", "pip", "install", "--upgrade", "specbutler")


def test_detect_installation_collects_repository_urls_from_distribution_metadata():
    dist = FakeDistribution(
        {
            "INSTALLER": "pip\n",
        },
        metadata_values={"Project-URL": ["Homepage, https://example.com", "Repository, https://github.com/acme/spec"]},
    )

    with (
        patch("spec_runtime.update._read_distribution", return_value=dist),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
    ):
        info = detect_installation()

    assert info.source_urls == ("https://example.com", "https://github.com/acme/spec")


def test_resolve_repo_slug_falls_back_to_direct_url_when_origin_missing(tmp_path):
    with patch("spec_runtime.update.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "missing")):
        slug = resolve_repo_slug(
            tmp_path,
            {"url": "git+https://github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
        )

    assert slug == "acme/spec"


def test_resolve_repo_slug_prefers_direct_url_over_origin(tmp_path):
    with patch(
        "spec_runtime.update.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "git@github.com:myfork/spec.git\n", ""),
    ):
        slug = resolve_repo_slug(
            tmp_path,
            {"url": "git+https://github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
            source_urls=("https://github.com/example/other-spec.git",),
        )

    assert slug == "acme/spec"


def test_resolve_repo_slug_uses_origin_when_direct_url_is_file_url(tmp_path):
    with patch(
        "spec_runtime.update.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "git@github.com:acme/spec.git\n", ""),
    ):
        slug = resolve_repo_slug(
            tmp_path,
            {"url": "file:///local/path", "dir_info": {}},
        )

    assert slug == "acme/spec"


def test_resolve_repo_slug_uses_origin_for_editable_installs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch(
        "spec_runtime.update.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "git@github.com:acme/spec.git\n", ""),
    ) as run_mock:
        slug = resolve_repo_slug(
            workspace,
            {"url": (tmp_path / "spec-src").as_uri(), "dir_info": {"editable": True}},
        )

    assert slug == "acme/spec"
    assert run_mock.call_args.kwargs["cwd"] == workspace


def test_resolve_repo_slug_editable_prefers_origin_over_source_urls(tmp_path):
    """For editable/local installs, origin should win over source_urls metadata."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch(
        "spec_runtime.update.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "git@github.com:myfork/spec.git\n", ""),
    ):
        slug = resolve_repo_slug(
            workspace,
            {"url": (tmp_path / "spec-src").as_uri(), "dir_info": {"editable": True}},
            source_urls=("https://github.com/upstream/spec.git",),
        )

    assert slug == "myfork/spec"


def test_resolve_repo_slug_uses_distribution_metadata_when_origin_and_direct_url_are_unavailable(tmp_path):
    with patch(
        "spec_runtime.update.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, "", "missing"),
    ):
        slug = resolve_repo_slug(tmp_path, source_urls=("https://github.com/acme/spec.git",))

    assert slug == "acme/spec"


def test_resolve_repo_slug_skips_version_check_when_origin_and_direct_url_are_unavailable(tmp_path):
    with patch("spec_runtime.update.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "missing")):
        slug = resolve_repo_slug(tmp_path)

    assert slug is None


def test_fetch_latest_version_returns_highest_semver():
    payload = json.dumps(
        [
            {"tag_name": "v0.2.1"},
            {"tag_name": "v0.10.0"},
            {"tag_name": "not-a-version"},
            {"tag_name": "0.3.0"},
        ]
    ).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return payload

    with patch("spec_runtime.update.urllib_request.urlopen", return_value=Response()):
        latest = fetch_latest_version("acme/spec", token="")

    assert latest == "0.10.0"


def test_fetch_latest_version_filters_prereleases_by_default():
    payload = json.dumps(
        [
            {"tag_name": "v0.2.5.dev1", "prerelease": True},
            {"tag_name": "v0.2.5rc1", "prerelease": True},
            {"tag_name": "v0.2.4", "prerelease": False},
        ]
    ).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return payload

    with patch("spec_runtime.update.urllib_request.urlopen", return_value=Response()):
        latest = fetch_latest_version("acme/spec", token="")

    assert latest == "0.2.4"


def test_fetch_latest_version_includes_prereleases_when_requested():
    payload = json.dumps(
        [
            {"tag_name": "v0.2.5.dev1", "prerelease": True},
            {"tag_name": "v0.2.5rc1", "prerelease": True},
            {"tag_name": "v0.2.4", "prerelease": False},
        ]
    ).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return payload

    with patch("spec_runtime.update.urllib_request.urlopen", return_value=Response()):
        latest = fetch_latest_version("acme/spec", token="", include_prereleases=True)

    assert latest == "0.2.5rc1"


def test_fetch_latest_version_ignores_draft_and_orphan_tags():
    payload = json.dumps(
        [
            {"tag_name": "v9.0.0", "draft": True, "prerelease": False},
            # A bare tag is absent from the releases endpoint by definition.
            {"tag_name": "v0.3.0", "draft": False, "prerelease": False},
        ]
    ).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return payload

    with patch("spec_runtime.update.urllib_request.urlopen", return_value=Response()):
        latest = fetch_latest_version("acme/spec", token="")

    assert latest == "0.3.0"


def test_fetch_latest_version_paginates_across_all_release_pages():
    payloads = iter(
        [
            json.dumps([{"tag_name": "v0.2.0"}] * 100).encode("utf-8"),
            json.dumps([{"tag_name": "v0.9.0"}]).encode("utf-8"),
        ]
    )
    requested_urls: list[str] = []

    class Response:
        def __init__(self, payload: bytes):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        return Response(next(payloads))

    with patch("spec_runtime.update.urllib_request.urlopen", side_effect=fake_urlopen):
        latest = fetch_latest_version("acme/spec", token="")

    assert latest == "0.9.0"
    assert requested_urls == [
        "https://api.github.com/repos/acme/spec/releases?per_page=100&page=1",
        "https://api.github.com/repos/acme/spec/releases?per_page=100&page=2",
    ]


def test_write_and_read_update_cache_round_trip(tmp_path):
    cache_path = tmp_path / ".spec-state" / "update-check.json"
    checked_at = datetime(2026, 3, 26, 12, 0, tzinfo=UTC)

    write_update_cache(cache_path, "v0.2.5", checked_at=checked_at)
    entry = read_update_cache(cache_path)

    assert entry == UpdateCacheEntry(latest_version="0.2.5", checked_at=checked_at)


def test_write_and_read_update_cache_round_trip_for_failed_refresh(tmp_path):
    cache_path = tmp_path / ".spec-state" / "update-check.json"
    checked_at = datetime(2026, 3, 26, 12, 0, tzinfo=UTC)

    write_update_cache(cache_path, None, checked_at=checked_at)
    entry = read_update_cache(cache_path)

    assert entry == UpdateCacheEntry(latest_version=None, checked_at=checked_at)


def test_cache_is_fresh_uses_24_hour_ttl():
    entry = UpdateCacheEntry(
        latest_version="0.2.5",
        checked_at=datetime(2026, 3, 25, 12, 0, tzinfo=UTC),
    )

    assert cache_is_fresh(entry, now=datetime(2026, 3, 26, 11, 59, 59, tzinfo=UTC)) is True
    assert cache_is_fresh(entry, now=datetime(2026, 3, 26, 12, 0, 1, tzinfo=UTC)) is False


def test_maybe_print_update_notice_uses_fresh_cache(tmp_path, capsys):
    config = _config()
    cache_path = update_cache_path(tmp_path, config)
    write_update_cache(
        cache_path,
        "0.2.5",
        checked_at=datetime.now(UTC) - timedelta(hours=1),
    )

    with (
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
        patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn,
    ):
        maybe_print_update_notice(tmp_path, config)

    assert 'Update available: v0.2.0 → v0.2.5. Run "spec update" to upgrade.' in capsys.readouterr().err
    spawn_fn.assert_not_called()


def test_maybe_print_update_notice_uses_shared_cache_from_common_root(tmp_path, capsys):
    config = _config()
    common_root = tmp_path / "repo"
    worktree_root = common_root / ".worktrees" / "code-feature--token"
    worktree_root.mkdir(parents=True)
    cache_path = common_root / ".spec-state" / "update-check.json"
    write_update_cache(
        cache_path,
        "0.2.5",
        checked_at=datetime.now(UTC) - timedelta(hours=1),
    )

    with (
        patch(
            "spec_runtime.update.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, str(worktree_root) + "\n", ""),
                subprocess.CompletedProcess([], 0, str(common_root / ".git") + "\n", ""),
            ],
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.0"),
        patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn,
    ):
        maybe_print_update_notice(worktree_root, config)

    assert 'Update available: v0.2.0 → v0.2.5. Run "spec update" to upgrade.' in capsys.readouterr().err
    spawn_fn.assert_not_called()


def test_maybe_print_update_notice_skips_downgrade_for_newer_dev_build(tmp_path, capsys):
    config = _config()
    cache_path = update_cache_path(tmp_path, config)
    write_update_cache(
        cache_path,
        "0.2.4",
        checked_at=datetime.now(UTC) - timedelta(hours=1),
    )

    with (
        patch("spec_runtime.update.metadata.version", return_value="0.2.5.dev1"),
        patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn,
    ):
        maybe_print_update_notice(tmp_path, config)

    assert capsys.readouterr().err == ""
    spawn_fn.assert_not_called()


def test_maybe_print_update_notice_refreshes_stale_cache_in_background(tmp_path, capsys):
    config = _config()
    with patch("spec_runtime.update.resolve_common_root", return_value=tmp_path):
        cache_path = update_cache_path(tmp_path, config)
    write_update_cache(
        cache_path,
        "0.2.5",
        checked_at=datetime.now(UTC) - timedelta(days=2),
    )

    with (
        patch("spec_runtime.update.resolve_common_root", return_value=tmp_path),
        patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn,
    ):
        maybe_print_update_notice(tmp_path, config)

    assert capsys.readouterr().err == ""
    spawn_fn.assert_called_once()


def test_maybe_print_update_notice_coalesces_repeated_refreshes_while_lock_exists(tmp_path, capsys):
    config = _config()

    with (
        patch("spec_runtime.update.resolve_common_root", return_value=tmp_path),
        patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn,
    ):
        maybe_print_update_notice(tmp_path, config)
        maybe_print_update_notice(tmp_path, config)

    assert capsys.readouterr().err == ""
    spawn_fn.assert_called_once()


def test_maybe_print_update_notice_skips_refresh_when_state_dir_is_unwritable(tmp_path, capsys):
    config = _config()

    with (
        patch("spec_runtime.update.Path.mkdir", side_effect=PermissionError("read-only file system")),
        patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn,
    ):
        maybe_print_update_notice(tmp_path, config)

    assert capsys.readouterr().err == ""
    spawn_fn.assert_not_called()


def test_maybe_print_update_notice_respects_disable_switches(tmp_path, monkeypatch, capsys):
    config = _config(enabled=False)
    monkeypatch.setenv("SPEC_NO_UPDATE_CHECK", "1")

    with patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn:
        maybe_print_update_notice(tmp_path, config)

    assert capsys.readouterr().err == ""
    spawn_fn.assert_not_called()


def test_maybe_print_update_notice_does_not_retry_within_ttl_after_failed_refresh(tmp_path, capsys):
    config = _config()
    cache_path = update_cache_path(tmp_path, config)
    write_update_cache(
        cache_path,
        None,
        checked_at=datetime.now(UTC) - timedelta(hours=1),
    )

    with patch("spec_runtime.update._start_refresh_subprocess") as spawn_fn:
        maybe_print_update_notice(tmp_path, config)

    assert capsys.readouterr().err == ""
    spawn_fn.assert_not_called()


def test_refresh_update_cache_persists_failed_check_timestamp(tmp_path):
    config = _config()
    checked_at = datetime(2026, 3, 26, 12, 0, tzinfo=UTC)

    with (
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value=None),
        patch("spec_runtime.update._now_utc", return_value=checked_at),
    ):
        refresh_update_cache(tmp_path, config)

    assert read_update_cache(update_cache_path(tmp_path, config)) == UpdateCacheEntry(
        latest_version=None,
        checked_at=checked_at,
    )


def test_cmd_update_reports_already_latest(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.0"),
        patch("spec_runtime.update.subprocess.run") as run_mock,
    ):
        rc = cmd_update(object())

    assert rc == 0
    assert capsys.readouterr().out.strip() == "Already on the latest version (v0.2.0)"
    run_mock.assert_not_called()


def test_cmd_update_does_not_downgrade_newer_dev_build(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.5.dev1",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.4"),
        patch("spec_runtime.update.subprocess.run") as run_mock,
    ):
        rc = cmd_update(object())

    assert rc == 0
    assert capsys.readouterr().out.strip() == "Already on the latest version (v0.2.5.dev1)"
    run_mock.assert_not_called()


def test_cmd_update_runs_upgrade_and_reports_new_version(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.5"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess(["pip"], 0, "", ""),
        ) as run_mock,
        patch("spec_runtime.update.metadata.version", return_value="0.2.5"),
    ):
        rc = cmd_update(object())

    assert rc == 0
    output = capsys.readouterr().out
    assert f"{shlex.quote(sys.executable)} -m pip install --upgrade" in output
    assert "Updated Spec Butler from v0.2.0 to v0.2.5" in output
    run_mock.assert_called_once_with(install_info.upgrade_command, check=False, cwd="/")


def test_cmd_update_reports_installed_version_after_successful_upgrade(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.5"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess(["pip"], 0, "", ""),
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.4.dev1"),
    ):
        rc = cmd_update(object())

    assert rc == 0
    output = capsys.readouterr().out
    assert "Updated Spec Butler from v0.2.0 to v0.2.4.dev1" in output


def test_cmd_update_redacts_credentials_when_printing_git_upgrade_command(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+https://oauth2:secret-token@github.com/acme/spec.git",
        ),
        direct_url={
            "url": "https://oauth2:secret-token@github.com/acme/spec.git",
            "vcs_info": {"vcs": "git", "requested_revision": "main"},
        },
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.5"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess([sys.executable], 0, "", ""),
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.5"),
    ):
        rc = cmd_update(object())

    assert rc == 0
    output = capsys.readouterr().out
    assert "secret-token" not in output
    assert "oauth2" not in output
    assert "specbutler @ git+https://***@github.com/acme/spec.git" in output


def test_load_spec_runtime_config_parses_update_section(tmp_path, monkeypatch):
    config_path = tmp_path / ".spec.toml"
    config_path.write_text(
        """
[update]
check_enabled = false
""".strip()
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEC_CONFIG", str(config_path))

    parsed = load_spec_runtime_config()

    assert parsed.update.check_enabled is False


# --- Template drift check tests ---


def test_check_template_drift_detects_difference(tmp_path):
    """When a bundled template changed in the upgrade and repo-local differs, drift is detected."""
    (tmp_path / "AGENTS.md").write_text("old repo content")
    old_templates = {"AGENTS.md": "old bundled content"}

    def fake_check_output(cmd, *, text=False, stderr=None):
        # Post-upgrade bundled content differs from both old bundled and repo-local
        return "new bundled content"

    with patch("spec_runtime.update.subprocess.check_output", side_effect=fake_check_output):
        assert _check_template_drift(tmp_path, old_templates) is True


def test_check_template_drift_no_difference_when_bundled_unchanged(tmp_path):
    """When the bundled template didn't change in the upgrade, no drift even if repo differs."""
    (tmp_path / "AGENTS.md").write_text("customized content")
    old_templates = {"AGENTS.md": "same bundled content"}

    def fake_check_output(cmd, *, text=False, stderr=None):
        # Post-upgrade bundled content is same as pre-upgrade
        return "same bundled content"

    with patch("spec_runtime.update.subprocess.check_output", side_effect=fake_check_output):
        assert _check_template_drift(tmp_path, old_templates) is False


def test_check_template_drift_no_false_positive_for_customized_files(tmp_path):
    """Pre-existing user customizations do not trigger drift when bundled templates are unchanged."""
    (tmp_path / "AGENTS.md").write_text("user has heavily customized this file")
    (tmp_path / "CLAUDE.md").write_text("user added project-specific notes")
    old_templates = {"AGENTS.md": "bundled agents", "CLAUDE.md": "bundled claude"}

    def fake_check_output(cmd, *, text=False, stderr=None):
        # Bundled templates are the same after upgrade
        if "AGENTS.md" in cmd[-1]:
            return "bundled agents"
        if "CLAUDE.md" in cmd[-1]:
            return "bundled claude"
        return ""

    with patch("spec_runtime.update.subprocess.check_output", side_effect=fake_check_output):
        assert _check_template_drift(tmp_path, old_templates) is False


def test_check_template_drift_no_drift_when_repo_matches_new(tmp_path):
    """When bundled template changed but repo-local already matches new version, no drift."""
    (tmp_path / "AGENTS.md").write_text("new bundled content")
    old_templates = {"AGENTS.md": "old bundled content"}

    def fake_check_output(cmd, *, text=False, stderr=None):
        return "new bundled content"

    with patch("spec_runtime.update.subprocess.check_output", side_effect=fake_check_output):
        assert _check_template_drift(tmp_path, old_templates) is False


def test_check_template_drift_skips_missing_local_files(tmp_path):
    """When no repo-local templates exist, no drift and no error."""
    # tmp_path has no template files at all
    assert _check_template_drift(tmp_path, {}) is False


def test_cmd_update_prints_drift_warning_after_upgrade(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )
    old_tpl = {"AGENTS.md": "old"}

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.5"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess(["pip"], 0, "", ""),
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.5"),
        patch("spec_runtime.update._read_current_templates", return_value=old_tpl),
        patch("spec_runtime.update._check_template_drift", return_value=True) as drift_mock,
    ):
        rc = cmd_update(object())

    assert rc == 0
    drift_mock.assert_called_once_with(tmp_path, old_tpl)
    assert 'Templates have changed. Run "spec init --force" to refresh them.' in capsys.readouterr().err


def test_cmd_update_no_drift_warning_when_templates_match(tmp_path, capsys):
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.5"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess(["pip"], 0, "", ""),
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.5"),
        patch("spec_runtime.update._read_current_templates", return_value={}),
        patch("spec_runtime.update._check_template_drift", return_value=False),
    ):
        rc = cmd_update(object())

    assert rc == 0
    assert "spec init --force" not in capsys.readouterr().err


def test_cmd_update_skips_drift_check_when_version_unchanged(tmp_path, capsys):
    """No drift check when version didn't actually change (same version)."""
    install_info = InstallInfo(
        method="git",
        current_version="0.2.5",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=tmp_path),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.6"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess(["pip"], 0, "", ""),
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.5"),
        patch("spec_runtime.update._read_current_templates", return_value={}),
        patch("spec_runtime.update._check_template_drift") as drift_mock,
    ):
        rc = cmd_update(object())

    assert rc == 0
    drift_mock.assert_not_called()


def test_cmd_update_skips_drift_check_outside_repo(tmp_path, capsys):
    """No drift check when spec update runs outside a git repo."""
    install_info = InstallInfo(
        method="git",
        current_version="0.2.0",
        upgrade_command=(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "specbutler @ git+ssh://git@github.com/acme/spec.git",
        ),
        direct_url={"url": "ssh://git@github.com/acme/spec.git", "vcs_info": {"vcs": "git"}},
    )

    with (
        patch("spec_runtime.update.detect_installation", return_value=install_info),
        patch("spec_runtime.update._discover_repo_root", return_value=None),
        patch("spec_runtime.update.resolve_repo_slug", return_value="acme/spec"),
        patch("spec_runtime.update.fetch_latest_version", return_value="0.2.5"),
        patch(
            "spec_runtime.update.subprocess.run",
            return_value=subprocess.CompletedProcess(["pip"], 0, "", ""),
        ),
        patch("spec_runtime.update.metadata.version", return_value="0.2.5"),
        patch("spec_runtime.update._read_current_templates", return_value={}),
        patch("spec_runtime.update._check_template_drift") as drift_mock,
    ):
        rc = cmd_update(object())

    assert rc == 0
    drift_mock.assert_not_called()
    assert "spec init --force" not in capsys.readouterr().err
