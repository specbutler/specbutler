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
    & $FilePath @Arguments 2>&1 | Tee-Object -LiteralPath (Join-Path $evidenceRoot $LogName)
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)] [string] $LiteralPath,
        [Parameter(Mandatory = $true)] [string] $Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $encoding)
}

function Wait-ChatTurn {
    param([string] $SessionId, [hashtable] $Headers, [int] $TimeoutSeconds = 600)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $history = Invoke-RestMethod `
            -Uri "http://127.0.0.1:17702/api/v1/chat/sessions/$SessionId/history" `
            -Headers $Headers
        if (-not $history.turn_active) { return $history }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Chat turn timed out for session $SessionId"
}

function Get-LatestAssistantText {
    param($History)
    $assistantEntries = @($History.history | Where-Object { $_.role -eq 'assistant' })
    if ($assistantEntries.Count -eq 0) { return '' }
    $parts = [System.Collections.Generic.List[string]]::new()
    foreach ($entry in @($assistantEntries[-1])) {
        foreach ($event in $entry.events) {
            if ($event.kind -eq 'text' -and $event.text) { $parts.Add([string] $event.text) }
        }
    }
    return ($parts -join '')
}

$recordedRevision = (Get-Content -LiteralPath (Join-Path $sourceRoot '.lab-source-revision') -Raw).Trim()
if ($recordedRevision -ne $sourceRevision) { throw 'Staged source revision does not match proof configuration' }
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
$githubLogin = (& gh.exe api user --jq .login).Trim()
if ($LASTEXITCODE -ne 0 -or -not $githubLogin) { throw 'Unable to resolve authenticated GitHub login' }
& git.exe config user.name $githubLogin
& git.exe config user.email "$githubLogin@users.noreply.github.com"
& git.exe add .
& git.exe commit -m 'Create Windows lifecycle proof fixture'
if ($LASTEXITCODE -ne 0) { throw 'Initial fixture commit failed' }
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
install_command = '$wheelPython -m venv .venv; .venv\Scripts\python.exe -m pip install -e ".[dev]"'

[verify]

[[verify.gates]]
name = "test"
command = '.venv\Scripts\python.exe -m pytest -q'
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
& git.exe add .
& git.exe commit -m 'Configure Spec Butler proof lifecycle'
if ($LASTEXITCODE -ne 0) { throw 'Spec fixture commit failed' }
& git.exe push origin main
if ($LASTEXITCODE -ne 0) { throw 'Spec fixture push failed' }

Invoke-LoggedNative -FilePath $spec -Arguments @('doctor') -LogName 'spec-doctor.log'
Invoke-LoggedNative -FilePath $spec -Arguments @(
    'implement', '--spec', 'add-numbers', '--agent', 'codex', '--review-agent', 'codex'
) -LogName 'real-codex-lifecycle.log'
Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
    '-m', 'pytest', 'tests\test_calculator.py', '-q'
) -LogName 'fixture-final-test.log'

$mergedPulls = & gh.exe pr list --repo $repositorySlug --state merged --json number,url,mergedAt
if ($LASTEXITCODE -ne 0) { throw 'Unable to query merged proof pull request' }
$merged = @($mergedPulls | ConvertFrom-Json)
if ($merged.Count -lt 1) { throw 'Real Codex lifecycle did not leave a merged pull request' }
$worktreeBranches = @(& git.exe branch --list 'code/add-numbers--*')
if ($worktreeBranches.Count -ne 0) { throw 'Implementation branches remain after lifecycle cleanup' }

$webStarted = $false
$sessionIds = [System.Collections.Generic.List[string]]::new()
try {
    Invoke-LoggedNative -FilePath $spec -Arguments @(
        'web', 'start', '--background', '--host', '127.0.0.1', '--port', '17702'
    ) -LogName 'web-start.log'
    $webStarted = $true
    $token = (Get-Content -LiteralPath (Join-Path $fixtureRoot '.spec-state\web\auth-token') -Raw).Trim()
    if (-not $token) { throw 'Web authentication token was not created' }
    $headers = @{ Authorization = "Bearer $token" }
    $backends = Invoke-RestMethod -Uri 'http://127.0.0.1:17702/api/v1/chat/backends' -Headers $headers
    if (-not $backends.backends.codex) { throw 'Codex web chat backend is unavailable' }
    if ($backends.backends.claude) { throw 'Native Claude web chat must fail closed' }

    $marker = "WINDOWS-CONTEXT-$([Random]::new().Next(100000, 999999))"
    $createBody = @{
        mode = 'create'
        agent = 'codex'
        prompt = "Do not edit files. Remember the exact marker $marker. Reply ACK only."
    } | ConvertTo-Json -Compress
    $created = Invoke-RestMethod `
        -Method Post `
        -Uri 'http://127.0.0.1:17702/api/v1/chat/sessions' `
        -Headers $headers `
        -ContentType 'application/json' `
        -Body $createBody
    $sessionId = [string] $created.session_id
    $sessionIds.Add($sessionId)
    $first = Wait-ChatTurn $sessionId $headers
    if (-not (Get-LatestAssistantText $first)) { throw 'Initial Codex web chat turn returned no text' }

    $message = @{ text = 'What exact marker did I ask you to remember? Reply with the marker only.' } |
        ConvertTo-Json -Compress
    Invoke-WebRequest `
        -UseBasicParsing `
        -Method Post `
        -Uri "http://127.0.0.1:17702/api/v1/chat/sessions/$sessionId/messages" `
        -Headers $headers `
        -ContentType 'application/json' `
        -Body $message | Out-Null
    $second = Wait-ChatTurn $sessionId $headers
    $secondText = Get-LatestAssistantText $second
    if ($secondText -notmatch [regex]::Escape($marker)) {
        throw "Second Codex web chat turn lost context: $secondText"
    }
    $chatResult = [ordered]@{
        backend = 'codex'
        native_claude_available = $false
        retained_second_turn_context = $true
    } | ConvertTo-Json
    Write-Utf8NoBom -LiteralPath (Join-Path $evidenceRoot 'web-chat-result.json') -Value $chatResult
} finally {
    if ($webStarted) {
        foreach ($sessionId in $sessionIds) {
            try {
                Invoke-RestMethod `
                    -Method Post `
                    -Uri "http://127.0.0.1:17702/api/v1/chat/sessions/$sessionId/stop" `
                    -Headers $headers | Out-Null
            } catch {}
        }
        & $spec web stop 2>&1 | Add-Content -LiteralPath (Join-Path $evidenceRoot 'web-stop.log')
    }
}

$result = [ordered]@{
    status = 'passed'
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
}
$resultJson = $result | ConvertTo-Json
Write-Utf8NoBom -LiteralPath (Join-Path $evidenceRoot 'result.json') -Value $resultJson
$result | ConvertTo-Json
exit 0
