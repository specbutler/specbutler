---
id: doctor-gate-shell-operators
area: backend
priority: 30
depends_on: []
description: Make spec doctor reject verify gate commands that use shell syntax verify will not interpret
---

# Doctor Gate Shell Operators

## Goal

On POSIX, verify splits a `[[verify.gates]]` `command` into an argument list
unless the gate declares `shell` (`_verify_gate_command_args` and
`_selected_command_uses_typed_runtime` in `orchestrator.py`). `spec doctor`
instead reads the same string as a shell script (`_command_executables` in
`doctor.py`). So it reports `[ok]` for a gate like
`npm run typecheck && npm run check:type`. Verify then passes `&&`, `npm`, `run`
and `check:type` to npm as arguments. The gate fails, and the implementing agent
is sent off to "fix" code that was never broken (#100). Doctor should check a
gate the way verify will run it, and flag shell syntax that verify won't
interpret.

## Acceptance Criteria

- [ ] **Shell syntax in an argv-split gate is a doctor error.** On POSIX, a
  `[[verify.gates]]` gate with `command`, no `shell` and no `argv` gets a
  `[error]` check when the command contains shell syntax outside quotes:
  - the control operators `&&`, `||`, `;`, `|` and `&`;
  - redirections (`<`, `>`, `>>`, `2>&1` and similar);
  - subshell parentheses;
  - command substitution (`$(...)` or backticks);
  - parameter expansion (`$NAME` or `${NAME}`);
  - a leading `NAME=value` environment assignment.
  The message names the gate and the syntax it found. It says verify runs the
  command without a shell.
- [ ] **Fix hint.** The error's hint gives both fixes, using the gate's own
  location (`[[verify.gates]] name='<gate>'`): add `shell = "sh"` to the gate, or
  split it into one plain command per gate.
- [ ] **Quoted text is not shell syntax.** A gate whose operators appear only
  inside quotes doesn't get this error. Examples: `python -c "import a; a.run()"`
  and `pytest -k 'a or b'`. Neither do gates like the ones this repository uses
  (`.venv/bin/python -m pytest`, `.venv/bin/python -m ruff check .`).
- [ ] **Doctor agrees with verify.** A gate verify argv-splits is checked as an
  argument list: doctor resolves its first word, the program verify will launch,
  and nothing else. A gate with `shell = "sh"` keeps today's shell-script checks
  (`sh -n` syntax and every launcher in the script), and the same command that
  failed without `shell` passes with it. A gate with `argv` is never reported
  for shell syntax.
- [ ] **Bootstrap commands are unaffected.** Bootstrap commands run through a
  shell, so `python -m venv .venv && ...` stays valid. The native-Windows doctor
  path (`_windows_command_checks`) keeps its current behavior.
- [ ] **Docs.** `docs/getting-started.md` says, next to the `verify.gates`
  guidance, that a gate `command` runs without a shell unless it sets
  `shell = "sh"`.
- [ ] **Hermetic tests** in the doctor test suite cover:
  - the #100 command (`npm run typecheck && npm run check:type`) is an error with
    the fix hint;
  - the same command with `shell = "sh"` passes;
  - one test per syntax family above;
  - the quoted-operator commands and this repository's gates pass;
  - an `argv` gate passes;
  - a bootstrap command with `&&` is still accepted.
  Each new error test fails on `main` before the change.

## Out of Scope

- Changing how verify runs a gate. Legacy command strings stay argv-split, so a
  gate that works today keeps working the same way.
- `diagnostic_command`, implement setup/teardown hooks and the native-Windows
  command checks.
- Shell features that behave the same with or without a shell, such as globs and
  `~`. Doctor doesn't flag them.

## Design Notes

- Detection can reuse `shlex.shlex(..., posix=True, punctuation_chars=True)`.
  With `whitespace_split` on, quoted text stays inside one token, and an
  unquoted run of `();<>|&` comes back as its own token. Parameter and command
  substitution (`$`, backticks) aren't punctuation. Look for them while lexing,
  outside quotes, rather than on the joined token, so `'$HOME'` isn't flagged.
- `_command_checks` in `doctor.py` loops over `_configured_commands`. Gates are
  the entries with `validate_all_executables=True`. Give each gate's
  `VerifyGateConfig.command_variants` to the check, so it can tell `shell` and
  `argv` gates apart without parsing `.spec.toml` again.
- `tests/test_windows_fixture_parity.py` runs fake providers through
  `spec doctor`. Make sure its fixtures still pass.

## Agent Notes

- Read `doctor.py` (`_command_executables`, `_configured_commands`,
  `_command_checks`) and `orchestrator.py` (`_verify_gate_command_args`,
  `_selected_command_uses_typed_runtime`) first.
- Existing doctor tests show how to build a config and fake `run`/`which`.
  Find them with `grep -n "_command_checks\|verify command (" tests/`.
