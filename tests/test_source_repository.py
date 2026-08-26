from __future__ import annotations

from email.message import Message
from unittest.mock import patch

import pytest

from spec_runtime import source_repository


class FakeDistribution:
    def __init__(self, *, direct_url: str = "", project_urls: tuple[str, ...] = ()) -> None:
        self._direct_url = direct_url
        self.metadata = Message()
        for entry in project_urls:
            self.metadata["Project-URL"] = entry

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json":
            return self._direct_url or None
        return None


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("git@github.com:acme/spec.git", "https://github.com/acme/spec.git"),
        ("ssh://git@github.com/acme/spec", "https://github.com/acme/spec.git"),
        ("ssh://git@github.com:22/acme/spec", "https://github.com/acme/spec.git"),
        ("http://github.com:80/acme/spec", "https://github.com/acme/spec.git"),
        ("git+https://github.com/acme/spec.git", "https://github.com/acme/spec.git"),
        (
            "https://oauth2:secret@github.com/acme/spec.git",
            "https://github.com/acme/spec.git",
        ),
        ("https://[::1]:8443/acme/spec.git", "https://[::1]:8443/acme/spec.git"),
    ],
)
def test_normalize_repository_https_url(raw_url: str, expected: str) -> None:
    assert source_repository.normalize_repository_https_url(raw_url) == expected


@pytest.mark.parametrize(
    "raw_url",
    [
        "",
        "file:///tmp/spec",
        "javascript:alert(1)",
        "https://example.com/a b",
        "https://example.com/repository-only",
        "git@github.com#comment:acme/spec.git",
        "https://github.com/acme/spec.git#main",
        "https://github.com/acme/spec.git?token=secret",
        "https://github.com/acme/spec.git@main",
        "https://github.com/acme/spec;RUN",
    ],
)
def test_normalize_repository_https_url_rejects_unsafe_values(raw_url: str) -> None:
    with pytest.raises(ValueError):
        source_repository.normalize_repository_https_url(raw_url)


def test_runtime_repository_url_prefers_explicit_override() -> None:
    with (
        patch("spec_runtime.source_repository._source_checkout_repository_url") as checkout,
        patch("spec_runtime.source_repository._distribution_repository_url") as distribution,
    ):
        result = source_repository.runtime_repository_https_url(
            override="git@github.com:acme/spec-fork.git"
        )

    assert result == "https://github.com/acme/spec-fork.git"
    checkout.assert_not_called()
    distribution.assert_not_called()


def test_distribution_repository_url_prefers_vcs_direct_url() -> None:
    dist = FakeDistribution(
        direct_url='{"url":"ssh://git@github.com/acme/spec-fork.git","vcs_info":{"vcs":"git"}}',
        project_urls=("Repository, https://github.com/upstream/spec",),
    )
    with patch("spec_runtime.source_repository.metadata.distribution", return_value=dist):
        result = source_repository._distribution_repository_url()

    assert result == "https://github.com/acme/spec-fork.git"


def test_distribution_repository_url_uses_repository_project_metadata() -> None:
    dist = FakeDistribution(
        project_urls=(
            "Homepage, https://example.com/spec",
            "Repository, https://github.com/acme/spec",
        )
    )
    with patch("spec_runtime.source_repository.metadata.distribution", return_value=dist):
        result = source_repository._distribution_repository_url()

    assert result == "https://github.com/acme/spec.git"


@pytest.mark.parametrize("direct_url", ["[]", '"oops"'])
def test_distribution_repository_url_ignores_non_object_direct_url(direct_url: str) -> None:
    dist = FakeDistribution(
        direct_url=direct_url,
        project_urls=("Repository, https://github.com/acme/spec",),
    )
    with patch("spec_runtime.source_repository.metadata.distribution", return_value=dist):
        result = source_repository._distribution_repository_url()

    assert result == "https://github.com/acme/spec.git"


def test_distribution_repository_url_does_not_treat_homepage_as_clone_url() -> None:
    dist = FakeDistribution(project_urls=("Homepage, https://example.com/products/spec",))
    with patch("spec_runtime.source_repository.metadata.distribution", return_value=dist):
        result = source_repository._distribution_repository_url()

    assert result == ""


def test_runtime_repository_url_falls_back_from_checkout_to_distribution() -> None:
    with (
        patch("spec_runtime.source_repository._source_checkout_repository_url", return_value=""),
        patch(
            "spec_runtime.source_repository._distribution_repository_url",
            return_value="https://github.com/acme/spec.git",
        ),
    ):
        result = source_repository.runtime_repository_https_url()

    assert result == "https://github.com/acme/spec.git"
