---
id: spec-lint-and-authoring
area: backend
priority: 40
depends_on: []
description: Lint specs before running them, ship real authoring prompts, and allow spec plus implementation in one PR
---

# Spec Lint and Authoring

## Goal

Spec Butler's premise is a well-formed spec in. Yet nothing checks a spec before a
run, `spec init` ships no authoring prompt (repos without their own get a fallback
of two or three sentences), and every feature needs two PRs: one for the spec, one
for the implementation. Make spec quality checkable, give new repos good authoring
defaults, and allow a single PR when the operator prefers it.

## Acceptance Criteria

- [ ] **`spec lint [--spec ID] [--json]`** validates each spec and exits non-zero on
  errors.
  - **Errors:**
    - frontmatter fields and types are wrong (`id` must match the filename stem);
    - `depends_on` problems, using `validate_spec_graph` from #39;
    - the Goal or Acceptance Criteria sections are missing;
    - there are zero parseable acceptance criteria.
  - **Warnings:**
    - more than 8 criteria (suggest decomposing);
    - no Out of Scope section;
    - unknown frontmatter keys.
- [ ] **Runs refuse specs with lint errors.** `spec implement` and `spec auto run`
  refuse them with the lint output; warnings don't block. `spec doctor` includes a
  lint summary.
- [ ] **Bundled authoring prompts.** `src/spec_runtime/templates/` ships
  `spec-creation.md` and `task-scoping.md`. They're used whenever the repo has no
  `prompts/` override, and `spec init` copies them. Their guidance:
  - ask about intent first;
  - read AGENTS.md and the template;
  - explore the code before drafting;
  - specify outcomes, verification points and danger zones;
  - aim for at most 8 criteria;
  - leave publication to the operator.

  No project-specific content.
- [ ] **`spec lint --critique --spec ID`** runs the configured review agent read-only
  on the spec and prints structured suggestions: ambiguities, criteria that can't be
  tested, missing out-of-scope items, and likely decomposition. It never blocks.
- [ ] **Single-PR mode.** `[specs] require_merged_spec = false` lets
  `spec implement` run a spec that exists only on the local base checkout (not yet
  on the remote base). The spec file goes into the implementation PR. The
  `spec-pr-policy` workflow and its helper accept an implementation PR that adds its
  own spec file. The default stays `true`, which is today's behaviour.
- [ ] **Docs** (`README.md`, `docs/getting-started.md`) describe lint, critique and
  single-PR mode.
- [ ] **Hermetic tests cover:** lint errors and warnings on fixture specs, refusal in
  implement and autopilot, prompt fallback resolution, single-PR mode carrying the
  spec file, and the policy helper accepting it.

## Out of Scope

- Detecting specs whose change already landed by hand; that needs a separate design.
- Changing the spec format.

## Design Notes

- `validate_spec_graph` is in `src/spec_runtime/spec_metadata.py` (added by #39).
  Acceptance criteria extraction is `_extract_acceptance_checklist_items` in
  `src/spec_runtime/orchestrator.py`.
- The authoring prompt fallbacks are in `src/spec_runtime/orchestrator.py`
  (~6743–6750 and ~14903–14907).
- The policy workflow is `.github/workflows/spec-pr-policy.yml`; branch
  classification is in `src/spec_runtime/spec_identity.py`.
