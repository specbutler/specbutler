# Host verification evidence

Built-in reviewers cannot execute commands. They receive the canonical spec,
the exact diff, and the orchestrator's gate results. A gate can additionally
produce a bounded host-native receipt for review by declaring exact workspace
paths:

```toml
[[verify.gates]]
name = "windows-e2e"
argv_windows = ["powershell.exe", "-File", "tools/windows-e2e.ps1"]
command = "python tools/non_windows_e2e.py"
review_evidence = ["artifacts/windows-native-receipt.json"]
```

The gate command owns receipt creation. Each configured path must be a regular
UTF-8 file beneath the execution workspace, with no symlink, junction, reparse
point, or non-directory parent. A gate may declare at most eight files; each
file is limited to 64 KiB and all attachments across the review are limited to
256 KiB. Missing, unsafe, non-UTF-8, or oversized required evidence turns an
otherwise passing gate into an explicit failure.

On gate success, Spec Butler records the exact path, byte count, SHA-256 digest,
and verified HEAD. Immediately before built-in review, it rereads the file
without following the leaf link, verifies the captured size, digest, allowlist,
and reviewed revision, and redacts common structured and textual secret forms.
The prompt includes both source and redacted hashes. A changed or stale receipt
fails review preparation rather than silently omitting evidence. The reviewer
still has no command execution capability.

Receipt files normally contain status, native platform details, the command or
scenario identifier, and concise assertion results. Do not put credentials,
large logs, arbitrary user data, or instructions to the reviewer in a receipt.
Generated receipts must normally be in the repository's ignore rules so the
verify gate leaves the publishable worktree clean. A tracked receipt is usable
only when the gate does not modify it.

## Operator-supervised pre-commit handoff

Some native verification cannot safely run as an automated gate and must occur
before the implementation commit. Treat this as an explicit manual fallback,
not as a completed lifecycle phase:

1. State in the spec that the implementation agent must leave the candidate
   uncommitted and report `blocked` for host-native verification. Record the run
   ID and workspace shown by `spec status --spec <id>`.
2. Wait until the agent process has exited and the run is visibly blocked. From
   an operator shell, inspect the candidate and run the native verifier inside
   that exact workspace. Write only a bounded, sanitized receipt at the path
   declared by the relevant gate.
3. If verification fails, leave the run and its recovery material intact and
   resume it with `spec implement --spec <id> --run <run-id>` so the agent can
   address the failure. Do not alternate provider state roots while the run is
   active.
4. After native verification succeeds, commit the candidate from the operator
   shell, then resume the same run. The normal verify, publish, review, and merge
   phases must still run against that commit.

Do not edit run state, fabricate gate status, forge merge tags, or treat a
receipt as permission to skip re-verification. Preserve the workspace and
recovery journals until the resumed lifecycle has completed or the operator has
intentionally cleaned the run.
