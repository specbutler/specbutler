"""Tests for shared spec branch and PR identity helpers."""

from __future__ import annotations

from spec_runtime import spec_identity


class TestImplementationBranchIdentity:
    def test_code_branch_resolves_spec_id_and_token(self):
        identity = spec_identity.implementation_branch_identity("code/my-feature--20260310T120000")
        assert identity is not None
        assert identity.kind == "code"
        assert identity.spec_id == "my-feature"
        assert identity.run_token == "20260310T120000"

    def test_spec_authoring_branch_is_not_implementation(self):
        assert spec_identity.implementation_branch_identity("spec/my-feature") is None

    def test_specrun_branch_still_resolves_spec_id_and_token(self):
        identity = spec_identity.implementation_branch_identity("specrun/my-feature--20260310T120000")
        assert identity is not None
        assert identity.kind == "specrun"
        assert identity.spec_id == "my-feature"
        assert identity.run_token == "20260310T120000"

    def test_spec_authoring_branches_are_not_treated_as_implementation(self):
        assert spec_identity.implementation_branch_identity("specdoc/my-feature") is None
        assert spec_identity.implementation_branch_identity("spec/my-feature") is None
        assert spec_identity.implementation_branch_identity("spec-authoring/20260310T120000000000") is None
        assert spec_identity.is_specdoc_branch("specdoc/my-feature") is True
        assert spec_identity.is_specdoc_branch("spec/my-feature") is True
        assert spec_identity.is_authoring_branch("spec-authoring/20260310T120000000000") is True


class TestResolveSpecIdForPr:
    def test_spec_authoring_branch_does_not_resolve_as_implementation(self):
        assert spec_identity.resolve_spec_id_for_pr("spec/my-feature", "") is None

    def test_supports_code_branch_without_body_metadata(self):
        assert (
            spec_identity.resolve_spec_id_for_pr(
                "code/my-feature--20260310T120000",
                "",
            )
            == "my-feature"
        )

    def test_supports_specrun_branch_without_body_metadata(self):
        assert (
            spec_identity.resolve_spec_id_for_pr(
                "specrun/my-feature--20260310T120000",
                "",
            )
            == "my-feature"
        )

    def test_prefers_explicit_spec_id_metadata(self):
        body = "## Spec\nSpec-ID: explicit-spec\n[specs/explicit-spec.md](specs/explicit-spec.md)\n"
        assert (
            spec_identity.resolve_spec_id_for_pr(
                "code/my-feature--20260310T120000",
                body,
            )
            == "explicit-spec"
        )


class TestClassifyPrHeadRef:
    def test_classifies_code_branch_as_implementation(self):
        assert spec_identity.classify_pr_head_ref("code/my-feature--20260310T120000") == "implementation"

    def test_classifies_specdoc_branch_as_authoring(self):
        assert spec_identity.classify_pr_head_ref("specdoc/my-feature") == "authoring"

    def test_classifies_spec_branch_without_metadata_as_authoring(self):
        assert spec_identity.classify_pr_head_ref("spec/my-feature", "") == "authoring"

    def test_classifies_spec_branch_as_authoring_regardless_of_body(self):
        body = "## Spec\nSpec-ID: my-feature\n"
        assert spec_identity.classify_pr_head_ref("spec/my-feature", body) == "authoring"

    def test_classifies_anonymous_spec_authoring_branch_as_authoring(self):
        assert spec_identity.classify_pr_head_ref("spec-authoring/20260310T120000000000") == "authoring"

    def test_classifies_task_branch_as_task(self):
        assert spec_identity.classify_pr_head_ref("task/fix-thing--20260310T120000") == "task"


class TestReviewOwnershipMetadata:
    def test_extracts_local_review_owner(self):
        body = "## Review\nReview-Owner: local\n"
        assert spec_identity.extract_review_owner_from_pr_body(body) == "local"
        assert spec_identity.pr_body_uses_local_review(body) is True

    def test_review_owner_is_case_insensitive(self):
        body = "## Review\nReview-Owner: Local\n"
        assert spec_identity.extract_review_owner_from_pr_body(body) == "local"
        assert spec_identity.pr_body_uses_local_review(body) is True

    def test_missing_review_owner_fails_open_to_cloud_review(self):
        assert spec_identity.extract_review_owner_from_pr_body("") is None
        assert spec_identity.pr_body_uses_local_review("") is False

    def test_malformed_review_owner_fails_open_to_cloud_review(self):
        body = "## Review\nReview-Owner: local reviewer\n"
        assert spec_identity.extract_review_owner_from_pr_body(body) is None
        assert spec_identity.pr_body_uses_local_review(body) is False

    def test_formats_review_owner_marker(self):
        assert spec_identity.format_pr_review_owner() == "Review-Owner: local"
