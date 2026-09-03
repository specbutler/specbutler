# Native Windows support

Spec Butler's first native Windows tier is deliberately narrow. The supported
combination is **Windows 11, a repository on a local fixed NTFS volume, the
`worktree` execution backend, Codex, and Windows PowerShell**. Run the
orchestrator and the agent from the same Windows installation and user account.

## Support matrix

| Host and repository | Backend | Agent | Shell | Status |
|---|---|---|---|---|
| Windows 11; local fixed NTFS volume | `worktree` | Codex | Windows PowerShell 5.1 | Supported native tier |
| Windows 11; local fixed NTFS volume | `worktree` | Claude | PowerShell | Unavailable: fails closed before launch |
| WSL2 with the repository inside the Linux filesystem | Linux `worktree` | Codex or Claude | Linux shell | Linux-mode alternative; not native Windows |
| Supported Linux/macOS host | `container` | Codex or Claude | Worker shell | Supported backend alternative; see execution-backend guide |
| Windows 11 with Docker Desktop | `container` | Any | Any | Not claimed or release-tested |
| UNC, SMB, mapped network drive, or network-synchronized workspace | Any | Any | Any | Not supported |
| Windows Server | Any | Any | Any | CI coverage only; not the documented user tier |

Native Claude is unavailable because its required host sandbox is not available
on this tier. Spec Butler rejects the selection before launching Claude; it does
not weaken the sandbox policy. Use WSL2 with the repository in the WSL Linux
filesystem, a Linux VM, or a supported Linux/macOS container setup when Claude
is required.

Docker Desktop container execution on a Windows host and UNC/network
workspaces have not been qualified and must not be inferred from native Codex
support. Microsoft Excel, Office COM automation, and other desktop-application
control are outside Spec Butler's support scope; projects that require them need
their own Windows-native test and automation layer.

## Install on Windows 11

Install Python 3.11 or newer, Git for Windows, GitHub CLI, pipx, and a current
native Codex CLI. In PowerShell:

```powershell
py -3.12 -m pip install --user pipx
py -3.12 -m pipx ensurepath
# Open a new PowerShell after ensurepath updates PATH.

$SpecRelease = gh release view --repo specbutler/specbutler --json tagName --jq .tagName
pipx install "specbutler @ git+https://github.com/specbutler/specbutler.git@$SpecRelease"
# Optional browser dashboard and TUI:
pipx install --force "specbutler[web,tui] @ git+https://github.com/specbutler/specbutler.git@$SpecRelease"
```

Configure Git's long-path support from an elevated PowerShell once:

```powershell
git config --system core.longpaths true
```

Run the remaining setup and all Spec Butler commands from an ordinary,
non-elevated PowerShell session.

Keep the repository on a local NTFS drive (for example `C:\src\project`), then
authenticate and initialize it:

```powershell
gh auth login
codex                         # complete provider sign-in, then exit
cd C:\src\project
spec init
spec doctor
```

Review the generated `.spec.toml`. A Python project's Windows bootstrap and
gate commands should use the workspace-local virtual environment:

```toml
[execution]
backend = "worktree"

[agents]
default = "codex"
review_default = "codex"
allowed = ["codex"]

[bootstrap]
install_command = 'python -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"'
install_command_windows = 'python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ".[dev]"'
install_shell_windows = "powershell"

[verify]

[[verify.gates]]
name = "test"
command = '.venv/bin/python -m pytest'
argv_windows = [".venv/Scripts/python.exe", "-m", "pytest"]
parallel = true
```

The Windows-specific command and shell are required for a PowerShell script;
the portable `install_command` remains the POSIX fallback. The implementation
bootstrap may download dependencies. Built-in local reviewers do not rerun the
install or verification commands: Spec Butler passes them the canonical spec,
an exact host-materialized diff, and the gate results already produced by the
orchestrator. This avoids executing pull-request package hooks inside the
reviewer boundary.

Run a small spec manually before enabling unattended dispatch:

```powershell
spec doctor
spec create --spec windows-smoke --agent codex
# From the printed authoring worktree:
Set-Location -LiteralPath .worktrees\spec-windows-smoke
git status --short
git log -1 --stat
git push --set-upstream origin spec/windows-smoke
gh pr create --head spec/windows-smoke --base main
# Review and merge the spec PR, then return to the orchestration checkout.
Set-Location -LiteralPath ..\..
git pull --ff-only
spec implement --spec windows-smoke --agent codex --review-agent codex
spec status --spec windows-smoke
```

The authoring agent can edit and commit in its isolated worktree. Ordinary
GitHub environment credentials are omitted and Git publication is guarded;
network commands prompt for approval. Explicitly trusted user MCP servers
retain their own service authority and provider approval behavior. Push and
merge the spec branch from your normal PowerShell session before running
`spec implement` from the orchestration checkout.

Native Windows Codex runs copy OAuth state into a launch-scoped provider home.
Only one such OAuth-backed session per canonical Codex auth file can be active,
because Codex may rotate its refresh token. A concurrent launch fails promptly
rather than freezing another session or the web server. Configure
`OPENAI_API_KEY` or `CODEX_API_KEY` when parallel native Codex runs are needed.

## Troubleshooting

### `spec doctor` rejects the path

Confirm the repository is on a local fixed NTFS volume:

```powershell
$root = (git rev-parse --show-toplevel).Trim()
$drive = (Split-Path -Qualifier $root).TrimEnd(':')
Get-Volume -DriveLetter $drive | Select-Object DriveType, FileSystem, Path
```

Move the clone off UNC, SMB, mapped-network, removable, ReFS, FAT/exFAT, or
cloud-placeholder storage. WSL users should clone inside the WSL Linux
filesystem rather than `/mnt/c` when using the Linux-mode alternative.

### Git reports a path is too long

Enable `core.longpaths` from an elevated shell, keep the clone near the drive
root, and retry from a fresh worktree. Do not shorten or mutate Spec Butler's
recorded workspace paths by hand.

### Claude is reported as unavailable

This is an intentional fail-closed result on native Windows, not a missing PATH
entry. Select Codex, or run the repository in WSL2/Linux where Claude's required
host isolation is available. Do not bypass the preflight or change the sandbox
policy.

### A stopped run left state behind

Inspect before mutating:

```powershell
spec status --spec <id>
spec gc
spec gc --apply
```

The native process supervisor uses durable, authenticated control identity and
Windows Job Objects. Never terminate a raw PID copied from state. Preserve logs
and the run workspace until unpublished changes have been inspected.

Repository setup hooks may launch a background service and declare it in the
[implement setup manifest](setup-manifest.md). Spec Butler keeps the setup Job
alive after the setup command exits, authenticates each declared service by its
creation identity and kernel Job membership, and reaps the complete Job during
teardown. The handoff succeeds only after its cleanup registration is persisted;
otherwise the Job is terminated before the agent launches. A service that
deliberately breaks away from the setup Job is not a supported handoff and will
not receive raw-PID cleanup authority.

### Background web service or chat does not start

Install the `web` extra, run `spec web status`, and inspect `.spec-state\web`.
The service binds to loopback by default and requires its generated bearer
token. Native Codex chat is part of the documented tier; native Claude chat is
unavailable for the same sandbox reason as other Claude execution.

### Docker Desktop is installed but container doctor fails

The native Windows support claim covers only the `worktree` backend. Docker
Desktop container mode is not claimed. Use native worktree mode, or move the
orchestrator into WSL2/Linux and follow the container-backend guide there.

## Release evidence

Hosted Windows CI builds and installs the wheel, lints, runs the portable suite,
and runs native integration probes. The checked-in
[Windows 11 lab harness](../tools/windows-lab/README.md) adds a resettable KVM
proof with a real Codex lifecycle, disposable GitHub PR/merge, multi-turn web
chat, native process tests, and redacted machine-readable artifacts. The real
provider proof remains opt-in and separately marked so normal CI stays hermetic.
