$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$harnessRoot = 'C:\SpecHarness'
$config = Get-Content -LiteralPath (Join-Path $harnessRoot 'proof-config.json') -Raw | ConvertFrom-Json
$runName = [string] $config.run_name
$sourceRevision = [string] $config.source_revision
$githubOwner = [string] $config.github_owner
if ($runName -notmatch '^[a-z0-9][a-z0-9-]{0,47}$') { throw 'Invalid proof run name' }
if ($githubOwner -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$') { throw 'Invalid GitHub owner' }

$sourceRoot = Join-Path $harnessRoot 'source'
$evidenceRoot = Join-Path $harnessRoot "evidence\$runName"
$runRoot = Join-Path $harnessRoot "runs\$runName"
$venvRoot = Join-Path $harnessRoot 'venvs'
New-Item -ItemType Directory -Force -Path $evidenceRoot, $runRoot, $venvRoot | Out-Null

function Invoke-LoggedNative {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $LogName
    )
    # Windows PowerShell 5 promotes native stderr to NativeCommandError when
    # ErrorActionPreference is Stop. Healthy tools such as git emit progress on
    # stderr, so judge native commands by their exit code instead.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $FilePath @Arguments 2>&1 | Tee-Object -LiteralPath (Join-Path $evidenceRoot $LogName)
        $nativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($nativeExitCode -ne 0) {
        throw "$FilePath failed with exit code $nativeExitCode"
    }
}

function Invoke-NativeOutput {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $FilePath @Arguments)
        $nativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($nativeExitCode -ne 0) {
        throw "$FilePath failed with exit code $nativeExitCode"
    }
    return $output
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)] [string] $LiteralPath,
        [Parameter(Mandatory = $true)] [string] $Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $encoding)
}

function Wait-Condition {
    param(
        [Parameter(Mandatory = $true)] [scriptblock] $Condition,
        [Parameter(Mandatory = $true)] [string] $Description,
        [int] $TimeoutSeconds = 60
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for $Description"
}

function Wait-ProcessExit {
    param(
        [Parameter(Mandatory = $true)] [System.Diagnostics.Process] $Process,
        [Parameter(Mandatory = $true)] [string] $Description,
        [int] $TimeoutSeconds = 60
    )
    $exited = $Process.WaitForExit($TimeoutSeconds * 1000)
    if (-not $exited) { throw "$Description did not exit within $TimeoutSeconds seconds" }
    # Start-Process uses asynchronous readers for redirected streams. The
    # parameterless wait drains those readers and makes ExitCode available.
    $Process.WaitForExit()
    $Process.Refresh()
}

$evidenceClaims = [ordered]@{}
function Set-EvidenceClaim {
    param(
        [Parameter(Mandatory = $true)] [string] $Id,
        [Parameter(Mandatory = $true)] [string[]] $Evidence,
        [Parameter(Mandatory = $true)] [string] $Detail
    )
    $script:evidenceClaims[$Id] = [ordered]@{
        status = 'passed'
        evidence = $Evidence
        detail = $Detail
    }
    Write-Utf8NoBom `
        -LiteralPath (Join-Path $script:evidenceRoot 'evidence-claims.json') `
        -Value ($script:evidenceClaims | ConvertTo-Json -Depth 8)
}

$windowsIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$windowsPrincipal = New-Object System.Security.Principal.WindowsPrincipal($windowsIdentity)
$isElevated = $windowsPrincipal.IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)
$sessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
if ($isElevated) { throw 'Proof must run with a non-elevated user token' }
if ($sessionId -le 0) { throw 'Proof must run in an interactive Windows session' }
$userContext = [ordered]@{
    status = 'passed'
    non_elevated = $true
    interactive_session = $true
    session_id = $sessionId
}
Write-Utf8NoBom `
    -LiteralPath (Join-Path $evidenceRoot 'user-context.json') `
    -Value ($userContext | ConvertTo-Json -Depth 4)

$recordedRevision = (Get-Content -LiteralPath (Join-Path $sourceRoot '.lab-source-revision') -Raw).Trim()
if ($recordedRevision -ne $sourceRevision) { throw 'Staged source revision does not match proof configuration' }
if ($sourceRevision -notmatch '^[0-9a-f]{40}$') { throw 'Proof source revision must be an exact Git commit SHA' }
$sourceProvenance = [ordered]@{
    status = 'passed'
    configured_revision = $sourceRevision
    staged_revision = $recordedRevision
}
Write-Utf8NoBom `
    -LiteralPath (Join-Path $evidenceRoot 'source-provenance.json') `
    -Value ($sourceProvenance | ConvertTo-Json -Depth 4)
$windowsProduct = (Get-ComputerInfo -Property WindowsProductName).WindowsProductName
$systemVolume = Get-Volume -DriveLetter C
if ($windowsProduct -notmatch 'Windows 11') { throw "Proof requires Windows 11, found: $windowsProduct" }
if ($systemVolume.FileSystem -ne 'NTFS' -or $systemVolume.DriveType -ne 'Fixed') {
    throw "Proof requires a local fixed NTFS system volume, found: $($systemVolume.DriveType) $($systemVolume.FileSystem)"
}
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
    throw "Proof requires Windows PowerShell 5.1, found: $($PSVersionTable.PSVersion)"
}

$uv = 'C:\Tools\uv\uv.exe'
$codex = 'C:\Tools\Codex\codex.exe'
Invoke-LoggedNative -FilePath $codex -Arguments @('--version') -LogName 'codex-version.log'
Invoke-LoggedNative -FilePath 'gh.exe' -Arguments @('auth', 'status') -LogName 'gh-auth-status.log'

$sourceVenv = Join-Path $venvRoot 'source'
$wheelVenv = Join-Path $venvRoot 'wheel'
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $sourceVenv, $wheelVenv
Invoke-LoggedNative -FilePath $uv -Arguments @('venv', $sourceVenv, '--python', '3.12') -LogName 'source-venv.log'
$sourcePython = Join-Path $sourceVenv 'Scripts\python.exe'
Invoke-LoggedNative -FilePath $uv -Arguments @(
    'pip', 'install', '--python', $sourcePython, "$sourceRoot[dev,web,tui]"
) -LogName 'source-install.log'

$dist = Join-Path $runRoot 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
Invoke-LoggedNative -FilePath $sourcePython -Arguments @(
    '-m', 'build', '--wheel', '--outdir', $dist, $sourceRoot
) -LogName 'build-wheel.log'
$wheel = Get-ChildItem -LiteralPath $dist -Filter '*.whl' | Select-Object -First 1
if (-not $wheel) { throw 'Release-candidate wheel was not produced' }

Invoke-LoggedNative -FilePath $uv -Arguments @('venv', $wheelVenv, '--python', '3.12') -LogName 'wheel-venv.log'
$wheelPython = Join-Path $wheelVenv 'Scripts\python.exe'
Invoke-LoggedNative -FilePath $uv -Arguments @(
    'pip', 'install', '--python', $wheelPython, "$($wheel.FullName)[dev,web,tui]"
) -LogName 'wheel-install.log'
$spec = Join-Path $wheelVenv 'Scripts\spec.exe'
Invoke-LoggedNative -FilePath $spec -Arguments @('--version') -LogName 'spec-version.log'
Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
    '-m', 'pytest', (Join-Path $sourceRoot 'tests'), '-v'
) -LogName 'native-tests.log'
Set-EvidenceClaim `
    -Id 'runtime.native-suite' `
    -Evidence @('native-tests.log', 'wheel-install.log', 'spec-version.log') `
    -Detail 'The release-candidate wheel and complete candidate test tree ran under native Windows Python 3.12.'

$repositoryName = "specbutler-windows-$($runName.Replace('proof-', ''))"
$repositorySlug = "$githubOwner/$repositoryName"
$fixtureRoot = Join-Path $runRoot 'Repo ü Space'
New-Item -ItemType Directory -Force -Path (Join-Path $fixtureRoot 'tests') | Out-Null
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'calculator.py') -Value @'
def add(left: int, right: int) -> int:
    """Add two integers."""
    return left - right
'@
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'tests\test_calculator.py') -Value @'
from calculator import add


def test_adds_two_integers() -> None:
    assert add(2, 3) == 5
'@
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'pyproject.toml') -Value @'
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "specbutler-windows-proof-fixture"
version = "0.0.0"
requires-python = ">=3.11"

[project.optional-dependencies]
dev = ["pytest>=8.2"]

[tool.pytest.ini_options]
pythonpath = ["."]
'@

Set-Location $fixtureRoot
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @('init', '-b', 'main') -LogName 'fixture-git-init.log'
$githubLogin = (@(Invoke-NativeOutput -FilePath 'gh.exe' -Arguments @('api', 'user', '--jq', '.login')) -join "`n").Trim()
if (-not $githubLogin) { throw 'Unable to resolve authenticated GitHub login' }
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @('config', 'user.name', $githubLogin) -LogName 'fixture-git-user-name.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'config', 'user.email', "$githubLogin@users.noreply.github.com"
) -LogName 'fixture-git-user-email.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @('add', '.') -LogName 'fixture-git-add.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'commit', '-m', 'Create Windows lifecycle proof fixture'
) -LogName 'fixture-git-commit.log'
Invoke-LoggedNative -FilePath 'gh.exe' -Arguments @(
    'repo', 'create', $repositorySlug, '--private', '--source', $fixtureRoot, '--remote', 'origin', '--push'
) -LogName 'github-repository-create.log'

Invoke-LoggedNative -FilePath $spec -Arguments @('init') -LogName 'spec-init.log'
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot '.spec.toml') -Value @"
base_ref = "origin/main"

[paths]
specs_dir = "specs"
task_specs_dir = "specs/tasks"
state_dir = ".spec-state"
worktrees_dir = ".worktrees"

[retry]
cap = 5
no_progress_retry_threshold = 2

[agents]
default = "codex"
review_default = "codex"
allowed = ["codex"]

[bootstrap]
install_command = 'python -m venv .venv && .venv/bin/python -m pip install --no-build-isolation --no-deps -e .'
install_command_windows = '$wheelPython -m venv --system-site-packages .venv; C:\Windows\System32\cmd.exe /d /c "echo $wheelVenv\Lib\site-packages>.venv\Lib\site-packages\spec-proof-parent.pth"; .venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .'
install_shell_windows = "powershell"

[verify]

[[verify.gates]]
name = "test"
command = '.venv/bin/python -m pytest -q'
argv_windows = [".venv/Scripts/python.exe", "-m", "pytest", "-q"]
parallel = true
"@
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'specs\add-numbers.md') -Value @'
---
id: add-numbers
area: proof
priority: 1
depends_on: []
description: Correct the fixture integer addition implementation
---

# Correct integer addition

## Goal

Make `calculator.add(left, right)` return the arithmetic sum of its two integer arguments.

## Acceptance Criteria

1. `add(2, 3)` returns `5`.
2. The existing test passes without weakening or deleting it.
3. Commit the implementation and report completion through the Spec Butler contract.

## Out of Scope

- Additional calculator operations.
'@
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @('add', '.') -LogName 'lifecycle-git-add.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'commit', '-m', 'Configure Spec Butler proof lifecycle'
) -LogName 'lifecycle-git-commit.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @('push', 'origin', 'main') -LogName 'lifecycle-git-push.log'

Invoke-LoggedNative -FilePath $spec -Arguments @('doctor') -LogName 'spec-doctor.log'
Invoke-LoggedNative -FilePath $spec -Arguments @(
    'implement', '--spec', 'add-numbers', '--agent', 'codex', '--review-agent', 'codex'
) -LogName 'real-codex-lifecycle.log'
Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
    '-m', 'pytest', 'tests\test_calculator.py', '-q'
) -LogName 'fixture-final-test.log'

$mergedPulls = @(Invoke-NativeOutput -FilePath 'gh.exe' -Arguments @(
    'pr', 'list', '--repo', $repositorySlug, '--state', 'merged', '--json', 'number,url,mergedAt'
)) -join "`n"
$merged = @($mergedPulls | ConvertFrom-Json)
if ($merged.Count -lt 1) { throw 'Real Codex lifecycle did not leave a merged pull request' }
$worktreeBranches = @(Invoke-NativeOutput -FilePath 'git.exe' -Arguments @(
    'branch', '--list', 'code/add-numbers--*'
))
if ($worktreeBranches.Count -ne 0) { throw 'Implementation branches remain after lifecycle cleanup' }
$implementationWorktrees = @(
    Get-ChildItem -LiteralPath (Join-Path $fixtureRoot '.worktrees') -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'code-add-numbers--*' }
)
if ($implementationWorktrees.Count -ne 0) { throw 'Implementation workspaces remain after lifecycle cleanup' }
$provenanceRefs = @(Invoke-NativeOutput -FilePath 'git.exe' -Arguments @(
    'ls-remote', '--tags', 'origin', 'refs/tags/spec/merged/add-numbers', 'refs/tags/spec/merged/add-numbers^{}'
))
if ($provenanceRefs.Count -lt 1) { throw 'Lifecycle merge provenance tag is missing' }
$reviewDecisions = @(
    Get-ChildItem `
        -LiteralPath (Join-Path $fixtureRoot '.spec-state\runs') `
        -Filter 'review-decision.json' `
        -File `
        -Recurse `
        -ErrorAction SilentlyContinue
)
if ($reviewDecisions.Count -ne 1) {
    throw "Expected one retained local review decision, found $($reviewDecisions.Count)"
}
$reviewWarnings = @(
    Get-ChildItem `
        -LiteralPath (Join-Path $fixtureRoot '.spec-state\runs') `
        -Filter 'review-bootstrap-warning.json' `
        -File `
        -Recurse `
        -ErrorAction SilentlyContinue
)
if ($reviewWarnings.Count -ne 0) {
    throw 'Review bootstrap fell back to diff-only; release proof requires a clean isolated bootstrap'
}
$reviewDecision = Get-Content -LiteralPath $reviewDecisions[0].FullName -Raw | ConvertFrom-Json
if ($reviewDecision.status -ne 'approved' -and $reviewDecision.decision -ne 'approved') {
    throw 'Retained local review decision was not approved'
}
Copy-Item `
    -LiteralPath $reviewDecisions[0].FullName `
    -Destination (Join-Path $evidenceRoot 'review-decision.json')
$lifecycleResult = [ordered]@{
    status = 'passed'
    source_revision = $sourceRevision
    provider = 'codex'
    non_elevated = $true
    pull_request_merged = $true
    merged_pull_request = $merged[0].url
    provenance_tag_present = $true
    review_decision = 'approved'
    review_bootstrap_warning_present = $false
    implementation_branches_remaining = $worktreeBranches.Count
    implementation_workspaces_remaining = $implementationWorktrees.Count
}
Write-Utf8NoBom `
    -LiteralPath (Join-Path $evidenceRoot 'lifecycle-result.json') `
    -Value ($lifecycleResult | ConvertTo-Json -Depth 8)
Set-EvidenceClaim `
    -Id 'runtime.real-lifecycle' `
    -Evidence @('real-codex-lifecycle.log', 'fixture-final-test.log', 'result.json') `
    -Detail 'A real Codex implementation, review, disposable GitHub pull request, merge, and cleanup completed.'

$foregroundWeb = $null
try {
    $foregroundOut = Join-Path $evidenceRoot 'web-foreground.log'
    $foregroundErr = Join-Path $evidenceRoot 'web-foreground-error.log'
    $foregroundWeb = Start-Process `
        -FilePath $spec `
        -ArgumentList @('web', 'start', '--host', '127.0.0.1', '--port', '17701') `
        -WorkingDirectory $fixtureRoot `
        -RedirectStandardOutput $foregroundOut `
        -RedirectStandardError $foregroundErr `
        -PassThru
    Wait-Condition -Description 'foreground web readiness' -TimeoutSeconds 90 -Condition {
        try {
            $tokenPath = Join-Path $fixtureRoot '.spec-state\web\auth-token'
            if (-not (Test-Path -LiteralPath $tokenPath)) { return $false }
            $foregroundToken = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
            Invoke-RestMethod `
                -Uri 'http://127.0.0.1:17701/api/v1/chat/backends' `
                -Headers @{ Authorization = "Bearer $foregroundToken" } | Out-Null
            return $true
        } catch { return $false }
    }
    Invoke-LoggedNative -FilePath $spec -Arguments @('web', 'stop') -LogName 'web-foreground-stop.log'
    Wait-ProcessExit `
        -Process $foregroundWeb `
        -Description 'Foreground web process after spec web stop' `
        -TimeoutSeconds 30
} finally {
    if ($foregroundWeb -and -not $foregroundWeb.HasExited) {
        Stop-Process -Id $foregroundWeb.Id -Force -ErrorAction SilentlyContinue
    }
}

$webStarted = $false
$backgroundServerPid = 0
try {
    Invoke-LoggedNative -FilePath $spec -Arguments @(
        'web', 'start', '--background', '--host', '127.0.0.1', '--port', '17702'
    ) -LogName 'web-start.log'
    $webStarted = $true
    $tokenPath = Join-Path $fixtureRoot '.spec-state\web\auth-token'
    $supervisionPath = Join-Path $fixtureRoot '.spec-state\web\server.supervision.json'
    if (-not (Test-Path -LiteralPath $tokenPath)) { throw 'Web authentication token was not created' }
    if (-not (Test-Path -LiteralPath $supervisionPath)) { throw 'Web supervision token was not created' }
    $supervision = Get-Content -LiteralPath $supervisionPath -Raw | ConvertFrom-Json
    $backgroundServerPid = [int] $supervision.payload_identity.pid
    if ($backgroundServerPid -le 0) { throw 'Web supervision token has no payload process id' }
    Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
        (Join-Path $sourceRoot 'tools\windows-lab\runtime_proof.py'),
        'chat',
        '--base-url', 'http://127.0.0.1:17702',
        '--token-file', $tokenPath,
        '--evidence-root', $evidenceRoot,
        '--server-pid', [string] $backgroundServerPid
    ) -LogName 'web-chat-proof.log'
    $chatResult = Get-Content -LiteralPath (Join-Path $evidenceRoot 'web-chat-result.json') -Raw |
        ConvertFrom-Json
    if ($chatResult.status -ne 'passed') { throw 'Comprehensive real web chat proof did not pass' }
} finally {
    if ($webStarted) {
        try {
            Invoke-LoggedNative -FilePath $spec -Arguments @('web', 'stop') -LogName 'web-stop.log'
        } catch {
            $_ | Out-String | Add-Content -LiteralPath (Join-Path $evidenceRoot 'web-stop.log')
        }
    }
}
if ($backgroundServerPid -gt 0 -and (Get-Process -Id $backgroundServerPid -ErrorAction SilentlyContinue)) {
    throw "Background web process $backgroundServerPid survived spec web stop"
}
$webLifecycleResult = [ordered]@{
    status = 'passed'
    source_revision = $sourceRevision
    foreground = 'passed'
    background = 'passed'
    authenticated_readiness = $true
    remaining_server_and_action_processes = 0
}
Write-Utf8NoBom `
    -LiteralPath (Join-Path $evidenceRoot 'web-lifecycle-result.json') `
    -Value ($webLifecycleResult | ConvertTo-Json -Depth 6)
Set-EvidenceClaim `
    -Id 'runtime.web-lifecycle' `
    -Evidence @('web-foreground.log', 'web-foreground-stop.log', 'web-start.log', 'web-stop.log') `
    -Detail 'Foreground and durable background web services reached authenticated readiness and stopped with their owned processes gone.'
Set-EvidenceClaim `
    -Id 'runtime.web-chat' `
    -Evidence @('web-chat-result.json', 'web-chat-events.json', 'web-chat-proof.log') `
    -Detail 'Real Codex used three context-dependent turns over HTTP/SSE, reconnect/history, concurrent isolated sessions, and live cancellation without descendants.'
Set-EvidenceClaim `
    -Id 'runtime.native-claude-inventory' `
    -Evidence @('web-chat-result.json') `
    -Detail 'The native backend inventory offered Codex and failed closed for Claude.'
Set-EvidenceClaim `
    -Id 'runtime.web-integration-chat' `
    -Evidence @('web-chat-result.json', 'web-chat-events.json') `
    -Detail 'The proof used a real loopback listener, authenticated API requests, streaming reconnect, concurrent provider processes, and cancellation.'

Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
    (Join-Path $sourceRoot 'tools\windows-lab\runtime_proof.py'),
    'timeout-tree',
    '--work-root', (Join-Path $runRoot 'timeout-tree'),
    '--evidence-root', $evidenceRoot
) -LogName 'timeout-tree.log'
Set-EvidenceClaim `
    -Id 'runtime.timeout-cleanup' `
    -Evidence @('timeout-tree-result.json', 'timeout-tree.log') `
    -Detail 'A native parent-child-grandchild process tree exceeded a bounded timeout and every exact process identity was gone afterward.'

Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'pull', '--ff-only', 'origin', 'main'
) -LogName 'autopilot-git-pull.log'
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot '.spec.toml') -Value @'
base_ref = "origin/main"

[paths]
specs_dir = "specs"
task_specs_dir = "specs/tasks"
state_dir = ".spec-state"
worktrees_dir = ".worktrees"

[retry]
cap = 2
no_progress_retry_threshold = 2

[agents]
default = "codex"
review_default = "codex"
allowed = ["codex"]

[bootstrap]
argv_windows = ["C:\\Windows\\System32\\cmd.exe", "/d", "/c", "exit 0"]

[verify]
'@
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'specs\auto-root-a.md') -Value @'
---
id: auto-root-a
area: proof
priority: 1
depends_on: []
description: First ready autopilot proof root
---

# First ready autopilot proof root

## Goal

Exercise native autopilot supervision for one dependency-ready root.

## Acceptance Criteria

1. The proof agent starts and waits for the release marker.
'@
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'specs\auto-root-b.md') -Value @'
---
id: auto-root-b
area: proof
priority: 2
depends_on: []
description: Second ready autopilot proof root
---

# Second ready autopilot proof root

## Goal

Exercise native autopilot concurrency for a second ready root.

## Acceptance Criteria

1. The proof agent starts concurrently and waits for the release marker.
'@
Write-Utf8NoBom -LiteralPath (Join-Path $fixtureRoot 'specs\auto-dependent.md') -Value @'
---
id: auto-dependent
area: proof
priority: 3
depends_on:
  - auto-root-a
description: Autopilot proof dependent that must remain blocked
---

# Blocked autopilot proof dependent

## Goal

Remain undispatched until the parent spec is merged.

## Acceptance Criteria

1. Autopilot does not dispatch this spec while `auto-root-a` is unresolved.
'@
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'add', '.spec.toml', 'specs'
) -LogName 'autopilot-git-add.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'commit', '-m', 'Add native autopilot proof graph'
) -LogName 'autopilot-git-commit.log'
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'push', 'origin', 'main'
) -LogName 'autopilot-git-push.log'

$shimRoot = Join-Path $runRoot 'autopilot-shim'
$agentEvents = Join-Path $runRoot 'autopilot-agent-events'
$releaseMarker = Join-Path $runRoot 'autopilot-release'
New-Item -ItemType Directory -Force -Path $shimRoot, $agentEvents | Out-Null
Remove-Item -LiteralPath $releaseMarker -Force -ErrorAction SilentlyContinue
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) {
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $csc)) { throw 'Windows C# compiler is unavailable for autopilot proof agent' }
Invoke-LoggedNative -FilePath $csc -Arguments @(
    '/nologo',
    '/target:exe',
    "/out:$(Join-Path $shimRoot 'codex.exe')",
    (Join-Path $sourceRoot 'tools\windows-lab\autopilot-agent.cs')
) -LogName 'autopilot-agent-build.log'

$oldPath = $env:Path
$oldProofEvents = $env:SPEC_AUTOPILOT_PROOF_EVENTS
$oldProofRelease = $env:SPEC_AUTOPILOT_PROOF_RELEASE
$oldProofSpec = $env:SPEC_AUTOPILOT_SPEC_EXE
$firstDispatcher = $null
$secondDispatcher = $null
$firstPids = [ordered]@{}
$activePath = Join-Path $fixtureRoot '.spec-state\autopilot\active.json'
$shutdownPath = Join-Path $fixtureRoot '.spec-state\autopilot\shutdown.json'
try {
    $env:Path = "$shimRoot;$(Join-Path $wheelVenv 'Scripts');$oldPath"
    $env:SPEC_AUTOPILOT_PROOF_EVENTS = $agentEvents
    $env:SPEC_AUTOPILOT_PROOF_RELEASE = $releaseMarker
    $env:SPEC_AUTOPILOT_SPEC_EXE = $spec

    $firstDispatcher = Start-Process `
        -FilePath $spec `
        -ArgumentList @('auto', 'run', '--concurrency', '2', '--poll-interval', '1') `
        -WorkingDirectory $fixtureRoot `
        -RedirectStandardOutput (Join-Path $evidenceRoot 'autopilot-first.log') `
        -RedirectStandardError (Join-Path $evidenceRoot 'autopilot-first-error.log') `
        -PassThru
    Wait-Condition -Description 'two autopilot implementation children' -TimeoutSeconds 120 -Condition {
        if (-not (Test-Path -LiteralPath $activePath)) { return $false }
        try {
            $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
            $properties = @($active.PSObject.Properties)
            if ($properties.Count -ne 2) { return $false }
            return @($properties | Where-Object { [int] $_.Value.pid -gt 0 }).Count -eq 2
        } catch { return $false }
    }
    Wait-Condition -Description 'two deterministic autopilot agents' -TimeoutSeconds 120 -Condition {
        return @(Get-ChildItem -LiteralPath $agentEvents -Filter 'started-*.json' -ErrorAction SilentlyContinue).Count -eq 2
    }
    $firstActive = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    foreach ($id in @('auto-root-a', 'auto-root-b')) {
        $entry = $firstActive.PSObject.Properties[$id].Value
        if (-not $entry) { throw "Expected active autopilot root is missing: $id" }
        $firstPids[$id] = [int] $entry.pid
    }
    if ($firstActive.PSObject.Properties['auto-dependent']) {
        throw 'Dependency-blocked autopilot spec was dispatched'
    }

    Stop-Process -Id $firstDispatcher.Id -Force
    Wait-ProcessExit `
        -Process $firstDispatcher `
        -Description 'Forced dispatcher' `
        -TimeoutSeconds 30
    foreach ($id in $firstPids.Keys) {
        if (-not (Get-Process -Id $firstPids[$id] -ErrorAction SilentlyContinue)) {
            throw "Adoptable implementation child for $id died with its dispatcher"
        }
    }

    $secondDispatcher = Start-Process `
        -FilePath $spec `
        -ArgumentList @('auto', 'run', '--concurrency', '2', '--poll-interval', '1') `
        -WorkingDirectory $fixtureRoot `
        -RedirectStandardOutput (Join-Path $evidenceRoot 'autopilot-second.log') `
        -RedirectStandardError (Join-Path $evidenceRoot 'autopilot-second-error.log') `
        -PassThru
    Wait-Condition -Description 'dispatcher adoption of both exact children' -TimeoutSeconds 60 -Condition {
        try {
            $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
            foreach ($id in @('auto-root-a', 'auto-root-b')) {
                $entry = $active.PSObject.Properties[$id].Value
                if (-not $entry) { return $false }
                if ([int] $entry.pid -ne [int] $firstPids[$id]) { return $false }
                if ([int] $entry.adoption_generation -ne 1) { return $false }
            }
            return $true
        } catch { return $false }
    }
    $adoptedActive = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    Write-Utf8NoBom `
        -LiteralPath (Join-Path $evidenceRoot 'autopilot-adopted-state.json') `
        -Value ($adoptedActive | ConvertTo-Json -Depth 12)

    $stopper = Start-Process `
        -FilePath $spec `
        -ArgumentList @('auto', 'stop') `
        -WorkingDirectory $fixtureRoot `
        -RedirectStandardOutput (Join-Path $evidenceRoot 'autopilot-stop.log') `
        -RedirectStandardError (Join-Path $evidenceRoot 'autopilot-stop-error.log') `
        -PassThru
    Wait-Condition -Description 'recorded graceful autopilot shutdown request' -TimeoutSeconds 15 -Condition {
        if (-not (Test-Path -LiteralPath $shutdownPath)) { return $false }
        try {
            $shutdownState = Get-Content -LiteralPath $shutdownPath -Raw | ConvertFrom-Json
            return $shutdownState.phase -in @('graceful', 'forced', 'complete')
        } catch { return $false }
    }
    $requestedShutdown = Get-Content -LiteralPath $shutdownPath -Raw | ConvertFrom-Json
    Write-Utf8NoBom `
        -LiteralPath (Join-Path $evidenceRoot 'autopilot-shutdown-requested-state.json') `
        -Value ($requestedShutdown | ConvertTo-Json -Depth 8)
    Write-Utf8NoBom -LiteralPath $releaseMarker -Value 'release'
    try {
        Wait-ProcessExit `
            -Process $stopper `
            -Description 'spec auto stop' `
            -TimeoutSeconds 60
    } catch {
        Stop-Process -Id $stopper.Id -Force -ErrorAction SilentlyContinue
        throw
    }
    Wait-ProcessExit `
        -Process $secondDispatcher `
        -Description 'Adopting dispatcher graceful shutdown' `
        -TimeoutSeconds 60
    $stopText = Get-Content -LiteralPath (Join-Path $evidenceRoot 'autopilot-stop.log') -Raw
    if ($stopText -notmatch 'acknowledged shutdown request') {
        throw 'spec auto stop did not record an acknowledged graceful shutdown'
    }
    $finalActive = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    if (@($finalActive.PSObject.Properties).Count -ne 0) {
        throw 'Autopilot active state was not empty after graceful shutdown'
    }
    foreach ($id in $firstPids.Keys) {
        if (Get-Process -Id $firstPids[$id] -ErrorAction SilentlyContinue) {
            throw "Implementation child for $id survived graceful auto stop"
        }
    }

    $startedEvents = @(
        Get-ChildItem -LiteralPath $agentEvents -Filter 'started-*.json' |
            ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json }
    )
    $finishedEvents = @(
        Get-ChildItem -LiteralPath $agentEvents -Filter 'finished-*.json' |
            ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json }
    )
    $startedIds = @($startedEvents | ForEach-Object { [string] $_.spec_id } | Sort-Object)
    if (($startedIds -join ',') -ne 'auto-root-a,auto-root-b') {
        throw "Autopilot launched an unexpected agent set: $($startedIds -join ',')"
    }
    if ($finishedEvents.Count -ne 2 -or @($finishedEvents | Where-Object { $_.report_exit_code -ne 0 }).Count) {
        throw 'Autopilot proof agents did not finish cleanly'
    }
    $adoptionLines = @(
        Select-String `
            -LiteralPath (Join-Path $evidenceRoot 'autopilot-second.log') `
            -Pattern 'adopt: auto-root-(a|b) '
    )
    if ($adoptionLines.Count -ne 2) {
        throw "Replacement dispatcher logged $($adoptionLines.Count) adoptions instead of exactly two"
    }
    $autoResult = [ordered]@{
        status = 'passed'
        concurrency = 2
        dispatched_specs = $startedIds
        blocked_dependent = 'auto-dependent'
        blocked_dependent_dispatch_count = 0
        forced_dispatcher_pid = $firstDispatcher.Id
        replacement_dispatcher_pid = $secondDispatcher.Id
        child_pids_before_restart = $firstPids
        child_pids_after_restart = [ordered]@{
            'auto-root-a' = [int] $adoptedActive.'auto-root-a'.pid
            'auto-root-b' = [int] $adoptedActive.'auto-root-b'.pid
        }
        adoption_generation = [ordered]@{
            'auto-root-a' = [int] $adoptedActive.'auto-root-a'.adoption_generation
            'auto-root-b' = [int] $adoptedActive.'auto-root-b'.adoption_generation
        }
        launches_per_spec = 1
        shutdown_phase_before_agent_release = [string] $requestedShutdown.phase
        graceful_auto_stop = $true
        remaining_agent_processes = 0
    }
    Write-Utf8NoBom `
        -LiteralPath (Join-Path $evidenceRoot 'autopilot-result.json') `
        -Value ($autoResult | ConvertTo-Json -Depth 10)
} finally {
    Write-Utf8NoBom -LiteralPath $releaseMarker -Value 'release'
    if ($secondDispatcher -and -not $secondDispatcher.HasExited) {
        try {
            Invoke-LoggedNative -FilePath $spec -Arguments @('auto', 'stop') -LogName 'autopilot-cleanup.log'
        } catch {
            $_ | Out-String | Add-Content -LiteralPath (Join-Path $evidenceRoot 'autopilot-cleanup.log')
        }
        try {
            Wait-ProcessExit `
                -Process $secondDispatcher `
                -Description 'Autopilot cleanup dispatcher' `
                -TimeoutSeconds 30
        } catch {
            Stop-Process -Id $secondDispatcher.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($firstDispatcher -and -not $firstDispatcher.HasExited) {
        Stop-Process -Id $firstDispatcher.Id -Force -ErrorAction SilentlyContinue
    }
    $env:Path = $oldPath
    $env:SPEC_AUTOPILOT_PROOF_EVENTS = $oldProofEvents
    $env:SPEC_AUTOPILOT_PROOF_RELEASE = $oldProofRelease
    $env:SPEC_AUTOPILOT_SPEC_EXE = $oldProofSpec
}
Set-EvidenceClaim `
    -Id 'runtime.autopilot' `
    -Evidence @('autopilot-result.json', 'autopilot-adopted-state.json', 'autopilot-shutdown-requested-state.json', 'autopilot-first.log', 'autopilot-second.log', 'autopilot-stop.log') `
    -Detail 'Two dependency-ready specs ran concurrently, the blocked dependent never launched, a replacement dispatcher adopted both exact children once, and auto stop drained them.'
Set-EvidenceClaim `
    -Id 'runtime.autopilot-restart' `
    -Evidence @('autopilot-result.json', 'autopilot-adopted-state.json') `
    -Detail 'The reusable Windows 11 lab forced dispatcher death and proved durable native adoption and graceful stop with real subprocesses.'

$result = [ordered]@{
    status = 'evidence-collected'
    acceptance_status = 'requires-fail-closed-audit'
    acceptance_manifest = 'tools/windows-lab/acceptance-manifest.json'
    run_name = $runName
    source_revision = $sourceRevision
    windows_edition = $windowsProduct
    filesystem = $systemVolume.FileSystem
    backend = 'worktree'
    agent = 'codex'
    shell = 'Windows PowerShell'
    github_repository = $repositorySlug
    merged_pull_request = $merged[0].url
    native_suite = 'passed'
    real_codex_lifecycle = 'passed'
    real_web_chat_context = 'passed'
    real_web_chat_dependent_turns = 3
    web_chat_reconnect = 'passed'
    web_chat_concurrent_isolation = 'passed'
    web_chat_cancellation_cleanup = 'passed'
    timeout_tree_cleanup = 'passed'
    autopilot_dependency_dispatch = 'passed'
    autopilot_restart_adoption = 'passed'
    autopilot_stop = 'passed'
    evidence_claims = $evidenceClaims
}
$resultJson = $result | ConvertTo-Json -Depth 12
Write-Utf8NoBom -LiteralPath (Join-Path $evidenceRoot 'result.json') -Value $resultJson
$result | ConvertTo-Json
exit 0
