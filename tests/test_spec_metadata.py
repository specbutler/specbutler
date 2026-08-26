from __future__ import annotations

import logging
from pathlib import Path

from spec_runtime import spec_metadata


def _write_spec(path: Path, status_line: str = "") -> None:
    path.write_text(
        "---\n"
        "id: example\n"
        f"{status_line}"
        "area: orchestrator\n"
        "---\n"
        "# Example\n"
    )


def test_nonobsolete_legacy_status_warns_once_per_file(
    tmp_path: Path,
    caplog,
) -> None:
    spec_path = tmp_path / "example.md"
    _write_spec(spec_path, "status: not-started\n")
    spec_metadata._WARNED_LEGACY_STATUS_PATHS.clear()

    with caplog.at_level(logging.WARNING, logger="spec_runtime.spec_metadata"):
        first = spec_metadata.parse_spec_metadata(spec_path)
        second = spec_metadata.parse_spec_metadata(spec_path)

    assert first.obsolete is False
    assert second.obsolete is False
    warnings = [record.message for record in caplog.records if "legacy frontmatter" in record.message]
    assert len(warnings) == 1
    assert "status: not-started" in warnings[0]
    assert "status is derived from run state" in warnings[0]


def test_obsolete_legacy_status_remains_supported_without_warning(
    tmp_path: Path,
    caplog,
) -> None:
    spec_path = tmp_path / "example.md"
    _write_spec(spec_path, "status: obsolete\n")
    spec_metadata._WARNED_LEGACY_STATUS_PATHS.clear()

    with caplog.at_level(logging.WARNING, logger="spec_runtime.spec_metadata"):
        metadata = spec_metadata.parse_spec_metadata(spec_path)

    assert metadata.obsolete is True
    assert not caplog.records


def test_current_frontmatter_has_no_legacy_status_warning(
    tmp_path: Path,
    caplog,
) -> None:
    spec_path = tmp_path / "example.md"
    _write_spec(spec_path)
    spec_metadata._WARNED_LEGACY_STATUS_PATHS.clear()

    with caplog.at_level(logging.WARNING, logger="spec_runtime.spec_metadata"):
        metadata = spec_metadata.parse_spec_metadata(spec_path)

    assert metadata.obsolete is False
    assert not caplog.records
