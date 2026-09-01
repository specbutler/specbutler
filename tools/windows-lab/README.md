# Windows 11 release lab

This directory is a secret-free controller for a disposable, KVM-backed
Windows 11 VM. It exists to reproduce the native Spec Butler release proof; it
is not part of the installed `specbutler` package and it does not distribute
Windows or third-party tools.

The controller keeps all mutable or machine-specific material outside Git:
the Windows ISO, qcow2 disks, VM identity, unattended-install output, generated
password, SSH keys, downloaded installers, provider login, raw logs, and proof
artifacts. The adjacent `.gitignore` is defense in depth; never force-add those
paths.

## Host requirements

- A Linux x86-64 host with hardware virtualization enabled and `/dev/kvm`
  readable and writable by the operator.
- Docker Engine with Compose v2, SSH/SCP, Git, Python 3, `sha256sum`, and at
  least 40 GiB of free disk space (80 GiB is a practical minimum).
- A properly licensed or time-limited Windows 11 x64 ISO obtained from
  Microsoft. This repository does not supply an ISO or a license.
- Current Windows x64 artifacts for Git, GitHub CLI, uv, and Codex. Populate
  the manifest from the publishers' release pages, including independently
  verified SHA-256 hashes. For Codex installation and authentication options,
  use the [official Codex CLI documentation](https://developers.openai.com/codex/cli).

## First-time setup

```bash
cd tools/windows-lab
cp lab.env.example lab.env
cp toolchain.json.example toolchain.json
# Replace every angle-bracket placeholder in both local files.

./labctl init
./labctl up
./labctl wait                 # first Windows installation can take 15–45 minutes
./labctl provision
```

`init` verifies the ISO hash before creating an 80 GiB qcow2 disk, generates a
fresh local VM identity/password/SSH key, renders the unattended templates into
ignored state, builds the QEMU container, and creates the unattended media.
`provision` downloads only the HTTPS artifacts named in `toolchain.json`, checks
their pinned hashes on both host and guest, then installs the Windows toolchain.

Provider and forge authentication are deliberately manual and remain inside
the VM. Use the loopback-only noVNC console or SSH:

```bash
./labctl ssh
gh auth login
codex                         # complete the normal interactive sign-in
exit
./labctl shutdown
./labctl snapshot provisioned
```

The unattended lab user automatically logs into the loopback-only console so
interactive scheduled jobs can use provider login. Treat the VM as sensitive
local test infrastructure: do not expose the SSH, RDP, or noVNC ports, reuse
its generated password, or run untrusted source inside it.

## One-command release proof

After the baseline and provider/forge sign-in exist, one command resets the VM,
stages the configured tracked source revision, reprovisions idempotently, runs
the proof in the console session, and retrieves redacted evidence:

```bash
./labctl proof
```

The proof:

1. builds and installs both the candidate wheel and source distribution with
   the `dev`, `tui`, and `web` surfaces;
2. runs the full native test suite on Windows, then runs the installed-wheel
   CLI matrix again with its opt-in guard explicitly enabled;
3. creates a uniquely named private repository under `LAB_GITHUB_OWNER`;
4. runs a real Codex worktree lifecycle through implementation, local review,
   pull-request merge, and cleanup;
5. starts the native web service in foreground and background modes, then uses
   its authenticated HTTP/SSE API for three context-dependent real Codex turns,
   stream reconnect/history, two concurrent isolated chats, and live
   cancellation with exact descendant-process checks; native Claude remains
   explicitly unavailable;
6. runs a real native three-level timeout tree and proves every process identity
   is gone after bounded cleanup;
7. launches two dependency-ready specs under autopilot with a deterministic
   provider double, crashes the dispatcher while both implementation processes
   are live, proves a replacement adopts each exact child once without launching
   the blocked dependent, then exercises `spec auto stop` graceful draining;
8. retains the exact staged Git revision and sanitized logs under
   `tools/windows-lab/artifacts/<run-name>/`, then evaluates every one of the
   26 acceptance criteria in the three Windows specs against the checked-in
   evidence contract.

Before the controller audit, `local_acceptance.py` parses the two JUnit reports
and requires exact, unskipped test names for each local claim. It also validates
the real lifecycle and web result fields and actively probes executable
discovery, Windows path behavior, non-interactive watch, wheel/sdist imports,
`pip check`, warning-free `spec doctor`, documentation, and credential cleanup.
It writes each machine-readable local result only after that result's complete
prerequisite set passes. The host controller adds its own result only after the
clean-snapshot reset, staging, job execution, collection, and guest-side static
harness audit have all completed.

The deterministic autopilot provider is intentionally distinct from the real
Codex evidence: adoption must hold children at a reproducible boundary across a
forced dispatcher crash, while the lifecycle and chat portions independently
prove the candidate against the authenticated provider. The proof records this
distinction in `autopilot-result.json` and `web-chat-result.json`.

The disposable GitHub repository is retained so its merged PR is auditable;
delete it manually after retaining the release evidence. `proof` is intentionally
opt-in because it creates that external repository and consumes real provider
capacity. Microsoft and provider authentication may need renewal between runs.

Provisioning uses the administrative SSH control plane. Interactive proof jobs
use the logged-on account's filtered, non-elevated token and fail unless they are
in a real desktop session; this matches the documented day-to-day Windows tier.

Raw artifacts remain under ignored `state/raw/`; the publishable copy passes
through `redact.py`. The sanitized directory contains
`_redaction-report.json`, including the replacement count and a post-redaction
scan for every recognized credential shape. Redaction is a backstop, not
permission to print secrets.

To reuse one provisioned lab from multiple source worktrees, point the controller
at its absolute state directory instead of copying the VM disks:

```bash
SPEC_WINDOWS_LAB_STATE_ROOT=/absolute/path/to/windows-lab/state \
SPEC_WINDOWS_LAB_CONFIG=/absolute/path/to/lab.env \
SPEC_WINDOWS_TOOLCHAIN_CONFIG=/absolute/path/to/toolchain.json \
./labctl proof
```

The override is intentionally environment-only: it keeps the reusable disk,
identity, SSH key, and authentication state outside every source checkout while
each agent stages the exact commit from its own worktree.

### Fail-closed acceptance audit

`acceptance-manifest.json` is the release evidence contract. It repeats the
exact text and numbering from all three Windows specs and maps each criterion
to concrete retained artifacts and machine-evaluated assertions. The auditor
rejects criterion drift, missing or extra criteria, unsafe artifact paths,
malformed evidence, and any source revision other than the exact 40-character
commit staged into the guest.

The authoritative output is `acceptance-audit.json`, not the runtime summary
in `result.json`. A missing artifact is reported as `unproven`; a present
artifact that contradicts its assertion is `failed`. Both states produce a
nonzero exit status, and the final audit criterion is derived from the other
25 rather than asserted by the proof script. Therefore partial proof runs
cannot be mistaken for release approval.

The controller must run this audit after collection and redaction; a
`labctl proof` run is not release-passing unless the resulting
`acceptance-audit.json` has `status: passed`. To re-audit retained evidence
manually, check out its recorded source commit and run:

```bash
revision="$(git rev-parse HEAD)"
python3 tools/windows-lab/audit_acceptance.py \
  --manifest tools/windows-lab/acceptance-manifest.json \
  --source-root . \
  --evidence-root "tools/windows-lab/artifacts/<run-name>" \
  --expected-revision "$revision" \
  --output "tools/windows-lab/artifacts/<run-name>/acceptance-audit.json"
```

The caller must supply the revision independently from the collected evidence;
the auditor cross-checks it against both configured and staged revision values
in `source-provenance.json`. Retain CI result JSON files named by the manifest
alongside VM artifacts before the final audit. Do not synthesize a passing
result for unavailable hosted, provider, or platform evidence.

Hosted CI publishes those result files only after its complete Linux, macOS,
Windows wheel, Windows source-distribution, native integration, lint, and
installed-CLI matrices pass and a fail-closed aggregation job confirms every
fragment belongs to one workflow run and one exact checkout SHA. This includes
the static documentation and hermetic-test coverage reports; it does not claim
that the separately marked real-provider test passed. Download the combined
artifact and copy it into the sanitized VM evidence directory:

```bash
run_id=<github-actions-run-id>
ci_evidence="$(mktemp -d)"
gh run download "$run_id" \
  --pattern 'hosted-ci-acceptance-evidence-*' \
  --dir "$ci_evidence"
ci_index="$(find "$ci_evidence" -type f -name hosted-ci-evidence-index.json -print -quit)"
test -n "$ci_index"
python3 - "$ci_index" "$(git rev-parse HEAD)" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("status") != "passed" or payload.get("source_revision") != sys.argv[2]:
    raise SystemExit("hosted CI evidence does not match this checkout")
PY
find "$ci_evidence" -maxdepth 2 -type f -name '*-result.json' \
  -exec cp {} "tools/windows-lab/artifacts/<run-name>/" \;
```

GitHub pull-request workflows normally test GitHub's synthetic merge commit;
that exact revision is recorded in every report. It cannot be combined with VM
evidence from the pull-request head or a later squash commit. For final release
evidence, use the successful `main` push run for the same commit staged in the
VM, or deliberately stage the exact tested pull-request merge SHA.

If only per-job fragments were retained, the same strict aggregation can be
repeated from a checkout at their revision:

```bash
gh run download "$run_id" --pattern 'ci-evidence-*' --dir ci-fragments
python3 tools/ci_evidence.py aggregate \
  --input ci-fragments \
  --output hosted-ci-evidence \
  --source-root . \
  --expected-revision "$(git rev-parse HEAD)"
```

The local proof deliberately does not create
`hosted-windows-ci-result.json`, `hosted-windows-smoke-result.json`,
`cross-platform-lifecycle-result.json`, `cross-platform-web-result.json`, or
`linux-claude-web-result.json`. Those claims require their named hosted,
macOS/Linux, or real-Claude runs. Until independently produced artifacts for
the exact staged revision are retained beside the VM evidence, the fail-closed
audit reports those criteria as `unproven`.

## Controller commands

| Command | Purpose |
|---|---|
| `labctl init` | Validate inputs, render private state, and create the VM disk |
| `labctl up`, `down`, `shutdown`, `wait` | Start the VM, perform an emergency container stop, cleanly shut Windows down, or wait for SSH |
| `labctl provision` | Download, verify, and install the pinned guest toolchain |
| `labctl snapshot NAME` | Turn the stopped run disk into a read-only cold baseline and create a new overlay |
| `labctl reset NAME` | Replace the stopped run overlay; retain the prior overlay under ignored trash |
| `labctl stage [REF]` | Export tracked files from an exact Git commit into `C:\SpecHarness\source` |
| `labctl exec -- 'COMMAND'` | Run a foreground PowerShell command over SSH |
| `labctl job submit NAME FILE` | Run a PowerShell script in the logged-on console session |
| `labctl job wait NAME`, `job logs NAME` | Wait for or inspect a named job |
| `labctl collect NAME [DEST]` | Retrieve job/evidence files and apply redaction |
| `labctl proof` | Execute the complete reset-to-evidence release proof |
| `labctl logs`, `status`, `ssh` | Inspect the QEMU container or guest |

Snapshots are cold: shut Windows down before creating or resetting one. The
controller moves replaced overlays into ignored `state/trash/` instead of
deleting them. Remove old trash manually only after confirming it is not needed.

## Development checks

The normal test suite statically verifies required commands, placeholders,
ignored state, executable bits, documentation claims, and common credential
patterns. When available, also run:

```bash
shellcheck tools/windows-lab/labctl tools/windows-lab/entrypoint.sh
bash -n tools/windows-lab/labctl
sh -n tools/windows-lab/entrypoint.sh
pytest tests/test_windows_lab_harness.py
pytest tests/test_windows_acceptance_audit.py
pytest tests/test_ci_evidence.py
```

The real-provider pytest entrypoint is separately marked and skipped unless
both `SPEC_WINDOWS_REAL_PROVIDER=1` and an explicit
`SPEC_WINDOWS_LAB_CONFIG=/absolute/path/to/lab.env` are present:

```bash
SPEC_WINDOWS_REAL_PROVIDER=1 \
SPEC_WINDOWS_LAB_CONFIG="$PWD/tools/windows-lab/lab.env" \
pytest -m windows_real_provider tests/test_windows_real_provider.py
```
