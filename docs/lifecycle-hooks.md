# Run lifecycle hooks

A repository can have `spec` run its own command when a run starts, pauses,
continues or ends. Use this to keep an issue tracker, a chat channel or any
other shared board in step with what `spec` is doing, and to refuse a run that
someone else already owns.

Hooks are optional. Without a `[hooks]` table nothing runs and nothing is
recorded.

## Configuration

```toml
[hooks]
run_start       = "tracker claim {frontmatter_issue} --branch {branch} --session {run_id}"
run_needs_input = "tracker comment {frontmatter_issue} --file {message_file}"
run_blocked     = "tracker comment {frontmatter_issue} --file {message_file}"
run_resumed     = "tracker comment {frontmatter_issue} --file {message_file}"
run_finished    = "tracker close {frontmatter_issue} --file {message_file}"
run_abandoned   = "tracker release {frontmatter_issue} --file {message_file}"
timeout_seconds = 120   # per hook; the default
```

Configure only the events you need. Each value is a command string, split into
arguments without a shell, or an array of arguments. For shell syntax, call the
shell yourself: `run_finished = ["sh", "-c", 'tracker close "$SPEC_HOOK_FRONTMATTER_ISSUE"']`.

| Hook | Fires when | If it fails |
|------|-----------|-------------|
| `run_start` | A new run is about to be created by `spec implement`, `spec auto run` or `spec task` | **The run is refused.** No run record, branch or workspace exists yet |
| `run_needs_input` | The run stops and waits for `spec input` | Reported; the run keeps its state |
| `run_blocked` | The run stops blocked and needs an operator | Reported |
| `run_resumed` | A run that announced `needs_input` or `blocked` continues | Reported; the run continues |
| `run_finished` | The run completed every phase: the change is merged | Reported |
| `run_abandoned` | A run that announced a start is superseded by a new run, or `spec clean` removes it before it finished | Reported |

A failure is a non-zero exit, a timeout or a command that cannot start. A
failed `run_start` prints the command's last output and `spec implement` exits
1, so the hook's own message ("another owner already holds this") is what the
operator sees. Later failures are logged as warnings and never change the
run's state.

Automatic retries inside a run (verify, review, merge conflicts) fire nothing.
`spec stop` fires nothing either: a stopped run is still that run. `spec phase`
is a manual debugging command and fires no hooks.

When a new run replaces an older one, the older run's `run_abandoned` fires
before the new run's `run_start`, so a hook that holds a lock per run can
release the old one first.

## What a hook receives

Hooks run on the host, from the main checkout, with the environment of the
`spec` process: never inside an agent's workspace or sandbox, and always from
the main checkout's `.spec.toml` and files. Standard input is closed.

Every value is available both as a `{placeholder}` in the command's arguments
and as an environment variable:

| Placeholder | Environment variable | Value |
|-------------|---------------------|-------|
| `{event}` | `SPEC_HOOK_EVENT` | `start`, `needs_input`, `blocked`, `resumed`, `finished` or `abandoned` |
| `{spec_id}` | `SPEC_HOOK_SPEC_ID` | The spec id (`task-<token>` for a task run) |
| `{run_id}` | `SPEC_HOOK_RUN_ID` | The run id; stable for the run's whole life |
| `{branch}` | `SPEC_HOOK_BRANCH` | The run's branch |
| `{state}` | `SPEC_HOOK_STATE` | The run's status: `pending`, `waiting-for-input`, `blocked`, `passed`, `failed` |
| `{phase}` | `SPEC_HOOK_PHASE` | The phase the run is in |
| `{run_mode}` | `SPEC_HOOK_RUN_MODE` | `spec` or `task` |
| `{message_file}` | `SPEC_HOOK_MESSAGE_FILE` | A temporary file with one plain-text sentence about the event: the question for `needs_input`, the reason for `blocked` and `abandoned`. Deleted when the hook returns |
| `{frontmatter_<key>}` | `SPEC_HOOK_FRONTMATTER_<KEY>` | A scalar from the spec's frontmatter, for example `issue: TRACK-7` gives `{frontmatter_issue}` |

Free text (the agent's question, an error) is only ever passed through the
message file, with secrets redacted, so it cannot become part of a command
line.

Frontmatter comes from the spec as it was when the run started. A command that
uses `{frontmatter_<key>}` fails when the spec does not set that key; in
`run_start` this refuses the run, which is how a project requires every spec
to name its tracker issue. A task run has no frontmatter when it starts.
`spec lint` does not warn about a frontmatter key that a hook reads.

Other text in braces, such as `${HOME}` or an awk program, is left as written.

## Answering a run from outside

A `run_needs_input` hook can post the question wherever the owner will see it.
To continue the run with the answer, without a terminal:

```bash
spec input --spec <id> --message "<the answer>"
spec implement --spec <id>
```

`spec auto run` picks an answered run up on its own, so the second command is
only needed without the dispatcher.
