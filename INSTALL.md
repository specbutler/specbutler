# Installing Spec Butler

`spec` requires Python 3.11+, Git, an authenticated GitHub CLI (`gh`), and at
least one supported coding-agent CLI. Linux and macOS support Claude and Codex.
The supported native tier is Windows 11 on a local fixed NTFS repository using
the `worktree` backend, Codex, and PowerShell. Native Claude is unavailable;
UNC/network workspaces and Docker Desktop container mode are not claimed. See
[Native Windows support](docs/windows.md) for the exact matrix and limitations.

## Stable install

```bash
# Resolve and install the latest tagged GitHub Release in an isolated CLI env:
SPEC_RELEASE="$(gh release view --repo specbutler/specbutler --json tagName --jq .tagName)"
pipx install \
  "specbutler @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"

# Include the interactive TUI and browser dashboard:
pipx install --force \
  "specbutler[tui,web] @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"
```

Install pipx using your operating system's package manager or the [official
pipx instructions](https://pipx.pypa.io/stable/how-to/install-pipx.html).
`spec update` advances a tagged install to the newest non-prerelease version
tag.

On Windows 11, use PowerShell syntax:

```powershell
py -3.12 -m pip install --user pipx
py -3.12 -m pipx ensurepath
# Open a new PowerShell, then:
$SpecRelease = gh release view --repo specbutler/specbutler --json tagName --jq .tagName
pipx install "specbutler @ git+https://github.com/specbutler/specbutler.git@$SpecRelease"
pipx install --force "specbutler[tui,web] @ git+https://github.com/specbutler/specbutler.git@$SpecRelease"
```

## Development channel

To test the moving `main` branch, make that choice explicit:

```bash
pipx install --force \
  "specbutler[tui,web] @ git+https://github.com/specbutler/specbutler.git@main"
```

Refresh this channel by rerunning the same command. `spec update` advances
version-tag installs; it does not promise commit-level refreshes for a moving
branch whose package version has not changed.

For repository development, clone and use an isolated virtual environment:

```bash
git clone https://github.com/specbutler/specbutler.git
cd specbutler
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev,tui,web]"
```

The native Windows editable-install equivalents are
`.\.venv\Scripts\Activate.ps1` and
`.\.venv\Scripts\python.exe -m pip install -e ".[dev,tui,web]"`.

Verify both the package and external tools before initializing a project:

```bash
spec --version
git --version
gh auth status
claude --version  # or: codex --version; use Codex for native Windows
```

The project can also be installed from a wheel built with `python -m build`.
The wheel contains the templates and browser assets used at runtime. See
[Releasing and install channels](RELEASING.md) for the automated version, tag,
and GitHub Release flow.

## Quick start

```bash
cd /path/to/your-project
spec init
spec doctor                            # verify Git, agents, gh, commands, and paths
spec container doctor                  # only when using the container backend
spec create --spec my-feature
spec implement --spec my-feature
```

Advanced / debug:

```bash
spec phase --spec my-feature --phase verify   # run a single phase
spec analytics                                # summarize history
```

Continue with the [getting-started guide](docs/getting-started.md). It covers
configuration, execution backends, first-run checks, and recovery.

## Command name

The CLI command is `spec`. The distribution name is `specbutler` to
avoid a collision with the existing PyPI `spec` package. Stable releases are
installed from tagged GitHub source rather than PyPI.

## Architecture

The package is structured with clean adapter boundaries:

- **`spec_runtime.cli`** — public CLI entry point with intent-level commands
- **`spec_runtime.orchestrator`** — internal lifecycle engine (phases, retries, state)
- **`spec_runtime.forge`** — forge adapter protocol + GitHub implementation
- **`spec_runtime.agent_adapter`** — agent adapter protocol + Claude/Codex implementations
- **`spec_runtime.config`** — configuration loading from `.spec.toml`
