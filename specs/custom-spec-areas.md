---
id: custom-spec-areas
area: backend
priority: 10
depends_on: []
description: Let specs use repository-specific areas and stop linting non-spec markdown, so v0.6.0 dispatches them again
---

# Custom Spec Areas

## Goal

In v0.6.0, `spec_lint.py` rejects any `area` outside the four built-in areas
(`VALID_SPEC_AREAS`). Since #60, `spec_graph_skip_reasons` in `autopilot.py`
turns every lint error into a dispatch skip. So a repository that names its own
areas, such as Grove with `api`, `worker` or `web`, gets every open spec skipped
and an empty `spec auto run` queue (#124). v0.5.0 dispatched the same specs.

The area only matters to `select_agent`. An area missing from
`AREA_AGENT_MAP` already falls back to `[agents] default`, and that's the right
behavior. Separately, `lint_specs` lints a plain `specs/README.md` as if it were
a spec and reports it as a skipped spec "README". An unmapped area should be
advisory, and markdown that isn't a spec shouldn't be linted.

## Acceptance Criteria

- [ ] **An unmapped area is a warning.** `lint_specs` no longer reports an error
  for an `area` outside `VALID_SPEC_AREAS`. It reports a warning that names the
  area and the agent `select_agent` will use for it: `[agents] default` from the
  repository's config. A non-string `area` keeps its existing
  `area has an invalid type` error.
- [ ] **Dispatch and `spec implement` are unblocked.** A spec whose only lint
  finding is an unmapped area doesn't appear in `spec_graph_skip_reasons`. It is
  queued by `build_dispatch_queue` and dispatched to `[agents] default`, and
  `spec implement --spec <id>` doesn't refuse it at its lint checks in
  `orchestrator.py`. `spec list` doesn't list it under "Dependency problems".
- [ ] **The agent mapping is unchanged.** `AREA_AGENT_MAP` and `select_agent`
  keep their current behavior for the four built-in areas, for an `--agent`
  override, and for the UI heuristic.
- [ ] **Non-spec markdown isn't linted.** A `specs/*.md` file with no frontmatter
  block, such as a plain README, is skipped by `lint_specs` the way
  `iter_spec_metadata` skips it. It produces no `LintResult`, isn't counted in
  the `spec lint` summary, and never appears as a dispatch skip.
  `spec lint --spec README` reports `spec not found`, as for any non-spec. A file
  that has frontmatter but a missing or mismatched `id` is still linted and
  keeps its `id must match the filename stem` error.
- [ ] **Docs.** The `spec lint` paragraph in `docs/getting-started.md` says
  `area` is free-form. The four built-in areas pick an implementing agent, and
  any other area uses `[agents] default` with a lint warning.
- [ ] **Hermetic tests** in `tests/test_spec_lint.py` and the autopilot dispatch
  tests cover:
  - an unmapped area produces the warning and no error;
  - a spec with an unmapped area is queued and gets `[agents] default`, both
    `claude` and `codex`;
  - the built-in areas still map as before;
  - a frontmatter-less `README.md` is neither linted nor skipped;
  - a frontmatter file with a missing `id` still errors.
  Each new behavior test fails on `main` before the change.

## Out of Scope

- A repository-configured area list or area-to-agent map in `.spec.toml`. With
  unmapped areas allowed, none is needed for #124; it can be added later.
- Changing which agent the built-in areas pick.
- Linting or discovery of task specs under `specs/tasks/`.

## Design Notes

- The area check is in `lint_specs` (`spec_lint.py`), next to the other
  frontmatter checks. Resolve the fallback agent through
  `load_repo_spec_runtime_config(repo_root).agents.default`, which is what
  `select_agent` falls back to.
- For "has no frontmatter block", `parse_spec_frontmatter` returns `{}` when the
  first line isn't `---`, so check for a frontmatter block on the file itself
  rather than for an empty dict. A file with `---\n---` and no keys is still a
  broken spec.
- `TEMPLATE.md` stays excluded as it is today.

## Agent Notes

- Read `spec_lint.py` (`lint_specs`), `spec_metadata.py` (`iter_spec_metadata`,
  `parse_spec_frontmatter`, `VALID_SPEC_AREAS`), and `autopilot.py`
  (`spec_graph_skip_reasons`, `select_agent`, `AREA_AGENT_MAP`,
  `build_dispatch_queue`) first.
- `tests/test_spec_lint.py` has a parametrized frontmatter table that the new
  area cases fit into.
