---
id: spec-hold
area: backend
priority: 30
depends_on: []
description: Hold a spec out of spec auto run with a pushed spec/hold/<id> tag and spec hold / spec unhold
---

# Spec Hold

## Goal

Some specs must not run yet. The work may wait on something outside the
repository, such as a manual switch-on, or someone may already be building it
by hand. Today the only way to keep such a spec out of `spec auto run` is to
change what it means (`obsolete: true`, `superseded_by`), which takes a commit.
The other option is the undocumented `.spec-state/autopilot/spec-overrides.json`,
which only one checkout can see (#111). A pushed, per-spec git tag
`spec/hold/<id>` should hold the spec instead. It works like the
`spec/merged/<id>` completion tag, carries a reason, and every operator sees it
after a fetch.

## Acceptance Criteria

- [ ] **The hold tag.** A spec is held when the local tag
  `refs/tags/spec/hold/<id>` exists. Reachability from the base ref doesn't
  matter. `spec hold` writes an annotated tag whose message is a JSON object
  `{schema_version: 1, spec_id, reason, actor, timestamp}`. Merge tags use the
  same kind of JSON object (`spec_merge_tags.py`), and the actor is resolved the
  same way. The tag points at the base branch tip, so a plain `git fetch`
  follows it to other checkouts. A hold tag someone creates by hand (a
  lightweight tag, or a message that isn't valid JSON) still holds the spec, and
  its reason shows as `no reason recorded`.

  A hold removed elsewhere must disappear here too, and
  `git fetch --tags --prune` does not prune tags. So wherever specbutler
  already refreshes refs before reading status (`refresh_runtime_git_refs` for
  `spec auto run`, the fetch in `spec list`), it also fetches
  `+refs/tags/spec/hold/*:refs/tags/spec/hold/*` with `--prune`. That removes a
  local hold tag deleted on origin and leaves every other local tag alone.
  `spec implement` refreshes hold tags the same way, best effort, before its
  hold check. A failed refresh warns and falls back to local tags, as the
  merge-tag fetch in `check_dependencies_merged` does.
- [ ] **`spec hold` and `spec unhold`.** `spec hold <id> --reason TEXT` requires
  a non-blank `--reason`. It:
  - refuses an unknown spec id, a task spec, and a spec that is already merged
    or obsolete, with a clear error;
  - creates the tag and pushes it to `origin`, then prints the spec, the reason
    and the tag name;
  - replaces the tag and pushes it again when the spec is already held, and
    prints the reason it replaced;
  - exits non-zero when the push fails. It keeps the local tag in that case, and
    says the hold isn't shared yet and how to push it.

  `spec unhold <id>` deletes the tag on `origin`, then locally. When the
  remote delete fails, it keeps the local tag and exits non-zero. When the spec
  isn't held, it says so and exits 0.
- [ ] **Status.** `get_spec_status` returns a new `held` status, added to
  `VALID_STATUSES`. Order of precedence: `obsolete`, then `merged`, then
  `held`, then the existing statuses. So a held spec that has an active or
  failed run reads `held`. A spec that merged while a hold tag remained reads
  `merged`. `SpecGitState` (from `collect_git_spec_state`) carries the held
  spec ids and their parsed reasons, so callers don't read tags one at a time.

  `spec list` shows `held` in the STATUS column and keeps held specs visible
  without `--all`. Below the table, it adds a `Held (not dispatched):` section
  with each held spec, its reason, actor and timestamp, placed like the
  existing `Dependency problems` section.
  `spec status --spec <id>` prints `Status: held` and a `Hold:` line with the
  reason, actor and timestamp. The run details it prints today stay as they
  are.
- [ ] **Dispatch.** `build_dispatch_queue` never queues a held spec, whether it
  would be a new run or any kind of resume. When a spec becomes held, the
  `spec auto run` loop logs a `skip` line with its reason once, the same way
  graph skip reasons are logged. `spec auto run --dry-run` prints a
  `Held (not dispatched):` section listing each held spec and its reason after
  the queue. It prints that section even when the queue is empty. A run that
  was already active when the hold was set is not stopped (`spec stop` does
  that).
- [ ] **Dependents stay blocked.** A spec that depends on a held spec isn't
  dispatched, because `dependencies_are_merged` sees `held`, not `merged`.
  `check_dependencies_merged` in `orchestrator.py` reports the dependency as
  `<dep> (held: <reason>)`. That blocker text is what `spec implement` shows for
  the dependent. A hold changes nothing about how its dependents are
  unblocked: they still wait for the dependency's merge tag.
- [ ] **`spec implement` on a held spec.** `spec implement --spec <id>` refuses a
  held spec, for a new run and for a resume, unless `--ignore-hold` is given.
  The refusal names the reason and both ways forward: `spec unhold <id>`, or
  `--ignore-hold`. With `--ignore-hold` the run starts or resumes normally and
  the hold tag stays. The hold is checked when `spec implement` starts, not
  between phases. `--ignore-hold` doesn't bypass dependency checks, so a
  dependent of a held spec stays blocked. Autopilot never passes
  `--ignore-hold`.
- [ ] **Docs.**
  - `docs/getting-started.md` gets a section on holding a spec: when to use a
    hold rather than `obsolete` or `superseded_by`, `spec hold` and
    `spec unhold`, what a held spec does to its dependents, `--ignore-hold`, and
    how to create the tag by hand with plain git.
  - The `spec` CLI table in `AGENTS.md` gains rows for `spec hold` and
    `spec unhold`, and the `spec implement` row mentions `--ignore-hold`.
  - The note in `specs/TEMPLATE.md` about `obsolete: true` also points at
    `spec hold` for a temporary hold.
  - The `spec_status.py` module docstring lists `held`.
- [ ] **Hermetic tests**, using local bare repositories as `origin`, cover:
  - **Dispatch:** a held spec is left out of `build_dispatch_queue`, both as a
    new run and as a resumable run; `--dry-run` output lists it with its
    reason; and a hand-made lightweight hold tag also holds the spec.
  - **Status:** `held` precedence over in-progress and needs-attention, and
    below merged and obsolete; the `spec list` STATUS column and the held
    section; `spec status` shows the `Hold:` line.
  - **Dependents:** a dependent of a held spec is not dispatched;
    `check_dependencies_merged` reports `(held: <reason>)`; after
    `spec unhold`, the dependent is still blocked until the dependency merges.
  - **`spec implement`:** a held spec is refused without `--ignore-hold`, with the
    reason in the error, and starts with it.
  - **Commands:** `spec hold` creates and pushes the tag with the JSON message,
    replaces an existing hold, and refuses unknown, task, merged and obsolete
    specs. `spec unhold` deletes the tag locally and on the remote, and is a
    no-op for a spec that isn't held.
  - **Refresh:** after another clone runs `spec unhold`, the refresh drops the
    stale local hold tag and keeps unrelated local tags.
  Each new behavior test fails on `main` before the change.

## Out of Scope

- `.spec-state/autopilot/spec-overrides.json`. It stays as it is, beside the
  hold. It records a local "hide as obsolete", which the TUI reads and clears,
  and that means something different from a temporary hold. The hold neither
  reads nor writes it, and removing or migrating it is a separate change.
- Holding task specs under `specs/tasks/`.
- Stopping or pausing a run that is already active when the hold is set, and
  re-checking the hold between phases of a running workflow.
- Deleting a hold tag automatically when the held spec merges. `merged` takes
  precedence, so a leftover tag has no effect.
- Hold support in the TUI and web dashboards beyond what they get for free from
  `build_dispatch_queue` and `get_spec_status`.
- A `spec-backfill-merge-tags`-style audit for hold tags.

## Design Notes

- Put the tag helpers in a new `spec_hold_tags.py`, modeled on
  `spec_merge_tags.py`: the `spec/hold/` prefix, tag name, message build and
  parse, and the create, push and delete commands. A tolerant parse returns a
  `HoldTag` with an empty reason, not an error, for a tag that isn't valid
  JSON.
- `collect_git_spec_state` can list held specs with a single
  `git for-each-ref refs/tags/spec/hold/ --format=...` call that reads each
  tag's message (`%(contents)`), with no subprocess per spec.
- `get_spec_status` gives `held` only when the spec isn't obsolete and isn't
  merged. Put the check right after the `is_spec_merged` branch.
- `dependencies_are_merged` needs no change, since it compares against
  `merged`. The dependency-blocker reason text lives in
  `check_dependencies_merged` (via `read_spec_status`).
- Register `hold` and `unhold` as top-level subcommands in `cli.py`, next to
  `stop`, and add `--ignore-hold` to the `implement` parser. `cmd_run` in
  `orchestrator.py` is where `spec implement` refuses merged specs. The hold
  check belongs early on that path, before any lease, run or workspace is
  created.
- Resolve the base branch tip the way `refresh_merge_completion_state` does
  (`fence_base_ref`). Fetch first, so the tag points at a commit origin
  already has.

## Agent Notes

- Read `spec_merge_tags.py`, `spec_status.py` (`collect_git_spec_state`,
  `get_spec_status`, `refresh_merge_completion_state`), `autopilot.py`
  (`build_dispatch_queue`, `refresh_runtime_git_refs`, `render_queue` and the
  `--dry-run` branch of the run loop), `cli.py` (`_cmd_list`) and
  `orchestrator.py` (`check_dependencies_merged`, `cmd_run`, `cmd_status`)
  first.
- Existing tests that build bare-remote fixtures and merge tags are good
  models. Find them with `grep -rln "spec/merged/" tests/`.
- In a fresh repository with no commits, the tag can't point at a base tip.
  Fail with a clear error rather than tagging `HEAD`.
