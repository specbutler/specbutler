from __future__ import annotations

import logging
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "malicious_id",
    [
        'evil\" autofocus onfocus=\"alert(1)',
        "../outside",
        "Uppercase",
        "contains_underscore",
    ],
)
def test_parse_spec_metadata_rejects_noncanonical_id(
    tmp_path: Path,
    malicious_id: str,
) -> None:
    spec_path = tmp_path / "malicious.md"
    spec_path.write_text(f"---\nid: {malicious_id!r}\n---\n# Malicious\n")

    with pytest.raises(spec_metadata.InvalidSpecIdError, match="invalid frontmatter id"):
        spec_metadata.parse_spec_metadata(spec_path)


def test_iter_spec_metadata_ignores_invalid_id_and_keeps_valid_specs(
    tmp_path: Path,
    caplog,
) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "valid.md").write_text("---\nid: valid-spec\n---\n# Valid\n")
    (specs_dir / "malicious.md").write_text(
        "---\nid: 'evil\" onmouseover=\"alert(1)'\n---\n# Malicious\n"
    )

    with caplog.at_level(logging.WARNING, logger="spec_runtime.spec_metadata"):
        records = spec_metadata.iter_spec_metadata(tmp_path)

    assert [record.spec_id for record in records] == ["valid-spec"]
    assert "not a canonical slug" in caplog.text
