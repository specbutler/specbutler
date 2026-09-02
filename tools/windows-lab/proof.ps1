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
New-Item -ItemType Directory -Force -Path (Split-Path $evidenceRoot), (Split-Path $runRoot) | Out-Null
foreach ($freshRoot in @($evidenceRoot, $runRoot)) {
    if (Test-Path -LiteralPath $freshRoot) {
        throw "Proof run path already exists; refusing to mix evidence: $freshRoot"
    }
}
New-Item -ItemType Directory -Path $evidenceRoot, $runRoot | Out-Null
New-Item -ItemType Directory -Force -Path $venvRoot | Out-Null

function ConvertTo-NativeArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string] $Value
    )
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }

    $builder = New-Object System.Text.StringBuilder
    [void] $builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void] $builder.Append(('\' * (($backslashes * 2) + 1)))
            [void] $builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void] $builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void] $builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void] $builder.Append(('\' * ($backslashes * 2)))
    }
    [void] $builder.Append('"')
    return $builder.ToString()
}

function Get-NativeProcessTreeIdentities {
    param([Parameter(Mandatory = $true)] [int] $RootProcessId)
    $processes = @(Get-CimInstance -ClassName Win32_Process)
    $children = @{}
    foreach ($candidate in $processes) {
        $parent = [int]$candidate.ParentProcessId
        if (-not $children.ContainsKey($parent)) { $children[$parent] = @() }
        $children[$parent] += $candidate
    }
    $byPid = @{}
    foreach ($candidate in $processes) { $byPid[[int]$candidate.ProcessId] = $candidate }
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($RootProcessId)
    $seen = @{}
    $identities = @()
    while ($queue.Count -gt 0) {
        $processId = $queue.Dequeue()
        if ($seen.ContainsKey($processId)) { continue }
        $seen[$processId] = $true
        if ($byPid.ContainsKey($processId)) {
            $candidate = $byPid[$processId]
            $identities += [pscustomobject]@{
                process_id = $processId
                creation_date = [string]$candidate.CreationDate
            }
        }
        if ($children.ContainsKey($processId)) {
            foreach ($child in $children[$processId]) {
                $queue.Enqueue([int]$child.ProcessId)
            }
        }
    }
    return @($identities)
}

function Test-NativeProcessIdentityAlive {
    param([Parameter(Mandatory = $true)] $Identity)
    $current = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter "ProcessId = $([int]$Identity.process_id)" `
        -ErrorAction SilentlyContinue
    return (
        $null -ne $current -and
        [string]$current.CreationDate -eq [string]$Identity.creation_date
    )
}

function Stop-NativeProcessTree {
    param(
        [Parameter(Mandatory = $true)] [System.Diagnostics.Process] $Process,
        [Parameter(Mandatory = $true)] $Identities
    )
    $killerInfo = New-Object System.Diagnostics.ProcessStartInfo
    $killerInfo.FileName = "$env:SystemRoot\System32\taskkill.exe"
    $killerInfo.Arguments = "/PID $($Process.Id) /T /F"
    $killerInfo.UseShellExecute = $false
    $killerInfo.CreateNoWindow = $true
    $killerInfo.RedirectStandardOutput = $true
    $killerInfo.RedirectStandardError = $true
    $killer = New-Object System.Diagnostics.Process
    $killer.StartInfo = $killerInfo
    try {
        if (-not $killer.Start()) { throw 'taskkill did not start.' }
        $killerOutput = $killer.StandardOutput.ReadToEndAsync()
        $killerError = $killer.StandardError.ReadToEndAsync()
        if (-not $killer.WaitForExit(30000)) {
            $killer.Kill()
            [void]$killer.WaitForExit(30000)
            throw 'taskkill did not finish within 30 seconds.'
        }
        $killer.WaitForExit()
        [int]$killerExitCode = $killer.ExitCode
        $killerText = $killerOutput.Result + $killerError.Result
    } finally {
        $killer.Dispose()
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $survivors = @($Identities | Where-Object {
            Test-NativeProcessIdentityAlive -Identity $_
        })
        if ($survivors.Count -eq 0) { return }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    $survivorIds = @($survivors | ForEach-Object { $_.process_id }) -join ','
    throw (
        "taskkill exited $killerExitCode but exact native descendants remained: " +
        "$survivorIds; output: $($killerText.Trim())"
    )
}

function Write-NativeFailureLog {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [AllowEmptyString()] [string] $StandardOutput,
        [AllowEmptyString()] [string] $StandardError,
        [Parameter(Mandatory = $true)] [string] $Category
    )
    $safeName = [System.IO.Path]::GetFileNameWithoutExtension($FilePath) -replace '[^A-Za-z0-9._-]', '-'
    if (-not $safeName) { $safeName = 'native' }
    $name = "$Category-$safeName-$([Guid]::NewGuid().ToString('N')).log"
    Write-Utf8NoBom `
        -LiteralPath (Join-Path $evidenceRoot $name) `
        -Value ($StandardOutput + $StandardError)
    return $name
}

function Invoke-BoundedNativeProcess {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [int] $TimeoutSeconds
    )
    if ($TimeoutSeconds -le 0) { throw 'Native command timeout must be positive.' }
    $command = Get-Command -Name $FilePath -CommandType Application -ErrorAction Stop
    $argumentLine = (@($Arguments | ForEach-Object {
        ConvertTo-NativeArgument -Value $_
    }) -join ' ')
    $nativeTemp = Join-Path $runRoot 'native-command-temp'
    New-Item -ItemType Directory -Force -Path $nativeTemp | Out-Null
    $token = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $nativeTemp "$token.stdout"
    $stderrPath = Join-Path $nativeTemp "$token.stderr"
    $process = $null
    try {
        $process = Start-Process `
            -FilePath $command.Path `
            -ArgumentList $argumentLine `
            -WorkingDirectory (Get-Location).Path `
            -NoNewWindow `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
        # Windows PowerShell 5 can discard the native handle behind a
        # Start-Process object with redirected streams. Force it to materialize
        # before waiting so ExitCode remains an enforceable integer.
        $processHandle = $process.Handle
        if ($processHandle -eq [IntPtr]::Zero) {
            throw 'Started native process did not expose a usable process handle.'
        }
        $exited = $process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $exited) {
            # Snapshot exact PID/creation identities, bound taskkill itself,
            # and prove every observed descendant is gone. The outer
            # kill-on-close Job remains the final fail-closed boundary.
            $treeIdentities = @(Get-NativeProcessTreeIdentities -RootProcessId $process.Id)
            Stop-NativeProcessTree -Process $process -Identities $treeIdentities
            if (-not $process.WaitForExit(30000)) {
                throw "Timed-out native process tree $($process.Id) could not be terminated."
            }
        }
        $process.WaitForExit()
        [int] $nativeExitCode = $process.ExitCode
        $stdout = if (Test-Path -LiteralPath $stdoutPath) {
            [System.IO.File]::ReadAllText($stdoutPath)
        } else { '' }
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            [System.IO.File]::ReadAllText($stderrPath)
        } else { '' }
        return [pscustomobject]@{
            exit_code = if ($exited) { $nativeExitCode } else { 124 }
            timed_out = -not $exited
            standard_output = $stdout
            standard_error = $stderr
        }
    } catch {
        $supervisionFailure = $_
        $failedStdout = if (Test-Path -LiteralPath $stdoutPath) {
            Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue
        } else { '' }
        $failedStderr = if (Test-Path -LiteralPath $stderrPath) {
            Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
        } else { '' }
        $failureLog = Write-NativeFailureLog `
            -FilePath $FilePath `
            -StandardOutput ([string]$failedStdout) `
            -StandardError ([string]$failedStderr) `
            -Category 'native-supervision-failure'
        throw (
            "Native command supervision failed; diagnostic: $failureLog; " +
            $supervisionFailure.Exception.Message
        )
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $process) { $process.Dispose() }
    }
}

function Invoke-LoggedNative {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $LogName,
        [int] $TimeoutSeconds = 3600
    )
    $result = Invoke-BoundedNativeProcess `
        -FilePath $FilePath `
        -Arguments $Arguments `
        -TimeoutSeconds $TimeoutSeconds
    $combined = $result.standard_output + $result.standard_error
    Write-Utf8NoBom -LiteralPath (Join-Path $evidenceRoot $LogName) -Value $combined
    if ($combined) {
        Write-Output ($combined.TrimEnd([char[]]"`r`n"))
    }
    if ($result.timed_out) {
        throw "$FilePath timed out after $TimeoutSeconds seconds and its process tree was terminated; diagnostic: $LogName"
    }
    if ($result.exit_code -ne 0) {
        throw "$FilePath failed with exit code $($result.exit_code); diagnostic: $LogName"
    }
}

function Invoke-NativeOutput {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]] $Arguments,
        [int] $TimeoutSeconds = 300
    )
    $result = Invoke-BoundedNativeProcess `
        -FilePath $FilePath `
        -Arguments $Arguments `
        -TimeoutSeconds $TimeoutSeconds
    if ($result.timed_out) {
        $failureLog = Write-NativeFailureLog `
            -FilePath $FilePath `
            -StandardOutput $result.standard_output `
            -StandardError $result.standard_error `
            -Category 'native-output-timeout'
        throw "$FilePath timed out after $TimeoutSeconds seconds and its process tree was terminated; diagnostic: $failureLog"
    }
    if ($result.exit_code -ne 0) {
        $failureLog = Write-NativeFailureLog `
            -FilePath $FilePath `
            -StandardOutput $result.standard_output `
            -StandardError $result.standard_error `
            -Category 'native-output-failure'
        throw "$FilePath failed with exit code $($result.exit_code); diagnostic: $failureLog"
    }
    if (-not $result.standard_output) { return @() }
    return @($result.standard_output.TrimEnd([char[]]"`r`n") -split "`r?`n")
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)] [string] $LiteralPath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string] $Value
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
    source_revision = $sourceRevision
    user_name = $windowsIdentity.Name
    user_sid = $windowsIdentity.User.Value
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
$windowsOs = Get-CimInstance -ClassName Win32_OperatingSystem
$windowsProduct = [string]$windowsOs.Caption
$windowsBuildNumber = [int]$windowsOs.BuildNumber
$windowsProductType = [int]$windowsOs.ProductType
$systemVolume = Get-Volume -DriveLetter C
if ($windowsProductType -ne 1 -or $windowsBuildNumber -lt 22000) {
    throw "Proof requires a Windows 11 client build (build >= 22000, product type 1), found: $windowsProduct build $windowsBuildNumber product type $windowsProductType"
}
if ($systemVolume.FileSystem -ne 'NTFS' -or $systemVolume.DriveType -ne 'Fixed') {
    throw "Proof requires a local fixed NTFS system volume, found: $($systemVolume.DriveType) $($systemVolume.FileSystem)"
}
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
    throw "Proof requires Windows PowerShell 5.1, found: $($PSVersionTable.PSVersion)"
}

$uv = 'C:\Tools\uv\uv.exe'
$codex = 'C:\Tools\Codex\codex.exe'
$codexHost = 'C:\Tools\Codex\codex-code-mode-host.exe'
if (-not (Test-Path -LiteralPath $codexHost -PathType Leaf)) {
    throw "Codex code-mode host is missing from its required canonical path: $codexHost"
}
Invoke-LoggedNative -FilePath $codex -Arguments @('--version') -LogName 'codex-version.log'
Invoke-LoggedNative -FilePath 'gh.exe' -Arguments @('auth', 'status') -LogName 'gh-auth-status.log'
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = '0'
$env:GH_PROMPT_DISABLED = '1'
Invoke-LoggedNative -FilePath 'gh.exe' -Arguments @(
    'auth', 'setup-git', '--hostname', 'github.com'
) -LogName 'gh-auth-setup-git.log' -TimeoutSeconds 120
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'config', '--global', 'credential.interactive', 'false'
) -LogName 'git-disable-credential-prompts.log' -TimeoutSeconds 120

$sourceVenv = Join-Path $venvRoot 'source'
$wheelVenv = Join-Path $venvRoot 'wheel'
$sdistVenv = Join-Path $venvRoot 'sdist'
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $sourceVenv, $wheelVenv, $sdistVenv
Invoke-LoggedNative -FilePath $uv -Arguments @('venv', $sourceVenv, '--python', '3.12') -LogName 'source-venv.log'
$sourcePython = Join-Path $sourceVenv 'Scripts\python.exe'
Invoke-LoggedNative -FilePath $uv -Arguments @(
    'pip', 'install', '--python', $sourcePython, "$sourceRoot[dev,web,tui]"
) -LogName 'source-install.log'

$dist = Join-Path $runRoot 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
Invoke-LoggedNative -FilePath $sourcePython -Arguments @(
    '-m', 'build', '--wheel', '--sdist', '--outdir', $dist, $sourceRoot
) -LogName 'build-distributions.log'
$wheel = Get-ChildItem -LiteralPath $dist -Filter '*.whl' | Select-Object -First 1
if (-not $wheel) { throw 'Release-candidate wheel was not produced' }
$sdist = Get-ChildItem -LiteralPath $dist -Filter '*.tar.gz' | Select-Object -First 1
if (-not $sdist) { throw 'Release-candidate source distribution was not produced' }

Invoke-LoggedNative -FilePath $uv -Arguments @('venv', $wheelVenv, '--python', '3.12') -LogName 'wheel-venv.log'
$wheelPython = Join-Path $wheelVenv 'Scripts\python.exe'
Invoke-LoggedNative -FilePath $uv -Arguments @(
    'pip', 'install', '--python', $wheelPython, "$($wheel.FullName)[dev,web,tui]"
) -LogName 'wheel-install.log'
$sdistVenvLog = 'sdist-venv.log'
Invoke-LoggedNative -FilePath $uv -Arguments @('venv', $sdistVenv, '--python', '3.12') -LogName $sdistVenvLog
$sdistPython = Join-Path $sdistVenv 'Scripts\python.exe'
Invoke-LoggedNative -FilePath $uv -Arguments @(
    'pip', 'install', '--python', $sdistPython, "$($sdist.FullName)[dev,web,tui]"
) -LogName 'sdist-install.log'
$spec = Join-Path $wheelVenv 'Scripts\spec.exe'
Invoke-LoggedNative -FilePath $spec -Arguments @('--version') -LogName 'spec-version.log'
$oldSandboxProof = $env:SPEC_TEST_REVIEW_BOOTSTRAP_SANDBOX
try {
    $env:SPEC_TEST_REVIEW_BOOTSTRAP_SANDBOX = '1'
    Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
        '-m', 'pytest', (Join-Path $sourceRoot 'tests'), '-v',
        '--junitxml', (Join-Path $evidenceRoot 'native-tests.junit.xml')
    ) -LogName 'native-tests.log'
} finally {
    $env:SPEC_TEST_REVIEW_BOOTSTRAP_SANDBOX = $oldSandboxProof
}
$oldInstalledMatrix = $env:SPEC_WINDOWS_INSTALLED_CLI_MATRIX
$oldGithubWorkspace = $env:GITHUB_WORKSPACE
try {
    $env:SPEC_WINDOWS_INSTALLED_CLI_MATRIX = '1'
    $env:GITHUB_WORKSPACE = $sourceRoot
    Invoke-LoggedNative -FilePath $wheelPython -Arguments @(
        '-m', 'pytest', '-o', 'pythonpath=', '--import-mode=importlib',
        (Join-Path $sourceRoot 'tests\test_windows_probe.py::test_installed_artifact_cli_matrix'),
        '-v', '--junitxml', (Join-Path $evidenceRoot 'installed-cli-matrix.junit.xml')
    ) -LogName 'installed-cli-matrix.log'
} finally {
    $env:SPEC_WINDOWS_INSTALLED_CLI_MATRIX = $oldInstalledMatrix
    $env:GITHUB_WORKSPACE = $oldGithubWorkspace
}
Set-EvidenceClaim `
    -Id 'runtime.native-suite' `
    -Evidence @('native-tests.log', 'native-tests.junit.xml', 'installed-cli-matrix.log', 'installed-cli-matrix.junit.xml', 'wheel-install.log', 'sdist-install.log', 'spec-version.log') `
    -Detail 'The wheel, source distribution, full candidate test tree, and explicitly enabled installed-artifact CLI matrix ran under native Windows Python 3.12.'

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
    'repo', 'create', $repositorySlug, '--private'
) -LogName 'github-repository-create.log' -TimeoutSeconds 300
$expectedOrigin = "https://github.com/$repositorySlug.git"
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'remote', 'add', 'origin', $expectedOrigin
) -LogName 'github-origin-add.log' -TimeoutSeconds 120
$observedOrigin = (@(Invoke-NativeOutput -FilePath 'git.exe' -Arguments @(
    'remote', 'get-url', 'origin'
)) -join "`n").Trim()
if ($observedOrigin -cne $expectedOrigin) {
    throw "Disposable lifecycle origin is not the exact HTTPS URL: $observedOrigin"
}
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'push', '-u', 'origin', 'main'
) -LogName 'github-initial-push.log' -TimeoutSeconds 180

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
Invoke-LoggedNative -FilePath 'git.exe' -Arguments @(
    'push', 'origin', 'main'
) -LogName 'lifecycle-git-push.log' -TimeoutSeconds 180

Invoke-LoggedNative -FilePath $spec -Arguments @('doctor') -LogName 'spec-doctor.log'
Invoke-LoggedNative -FilePath $spec -Arguments @(
    'implement', '--spec', 'add-numbers', '--agent', 'codex', '--review-agent', 'codex'
) -LogName 'real-codex-lifecycle.log' -TimeoutSeconds 7200
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
$reviewEvidence = [ordered]@{
    status = 'approved'
    source_revision = $sourceRevision
    product_artifact = $reviewDecision
}
Write-Utf8NoBom `
    -LiteralPath (Join-Path $evidenceRoot 'review-decision.json') `
    -Value ($reviewEvidence | ConvertTo-Json -Depth 20)
$lifecycleResult = [ordered]@{
    status = 'passed'
    source_revision = $sourceRevision
    provider = 'codex'
    non_elevated = $true
    unattended_git_auth = $true
    raw_https_push = 'passed'
    native_command_timeouts = $true
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
    -Evidence @(
        'gh-auth-setup-git.log',
        'git-disable-credential-prompts.log',
        'github-origin-add.log',
        'github-initial-push.log',
        'lifecycle-git-push.log',
        'real-codex-lifecycle.log',
        'fixture-final-test.log',
        'lifecycle-result.json',
        'result.json'
    ) `
    -Detail 'Bounded, non-interactive Git authentication plus a real Codex implementation, review, disposable GitHub pull request, merge, and cleanup completed.'

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
    Invoke-LoggedNative -FilePath $spec -Arguments @('web', 'status') -LogName 'web-foreground-status.log'
    $foregroundStatus = Get-Content `
        -LiteralPath (Join-Path $evidenceRoot 'web-foreground-status.log') `
        -Raw
    if ($foregroundStatus -notmatch 'spec web is running') {
        throw 'A separate CLI process could not identify the foreground web ownership record'
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
        '--server-pid', [string] $backgroundServerPid,
        '--source-revision', $sourceRevision
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
    -Evidence @(
        'web-foreground.log',
        'web-foreground-status.log',
        'web-foreground-stop.log',
        'web-start.log',
        'web-stop.log'
    ) `
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
    '--evidence-root', $evidenceRoot,
    '--source-revision', $sourceRevision
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
        source_revision = $sourceRevision
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

$watchHarness = Join-Path $runRoot 'watch-conpty.exe'
Invoke-LoggedNative -FilePath $csc -Arguments @(
    '/nologo',
    '/target:exe',
    '/platform:x64',
    "/out:$watchHarness",
    (Join-Path $sourceRoot 'tools\windows-lab\watch-conpty.cs')
) -LogName 'watch-conpty-build.log'
$watchNonce = $sourceRevision.Substring(0, 16)
$watchSpecId = @(
    Invoke-NativeOutput -FilePath $wheelPython -Arguments @(
        '-I',
        '-c',
        'import pathlib,sys; from spec_runtime.autopilot_tui.dashboard import load_dashboard_snapshot; rows=load_dashboard_snapshot(pathlib.Path(sys.argv[1])).rows; print(rows[0].spec_id if rows else "")',
        $fixtureRoot
    )
) -join ''
$watchSpecId = $watchSpecId.Trim()
if ($watchSpecId -notin @('auto-root-a', 'auto-root-b')) {
    throw "Interactive spec watch proof found no live autopilot row: $watchSpecId"
}
Invoke-LoggedNative -FilePath $watchHarness -Arguments @(
    $spec,
    $fixtureRoot,
    $evidenceRoot,
    $sourceRevision,
    $watchSpecId,
    $watchNonce
) -LogName 'watch-conpty-proof.log'
$watchInteractive = Get-Content `
    -LiteralPath (Join-Path $evidenceRoot 'watch-interactive-result.json') `
    -Raw | ConvertFrom-Json
if (
    $watchInteractive.status -ne 'passed' `
    -or $watchInteractive.source_revision -ne $sourceRevision `
    -or $watchInteractive.pseudoconsole -ne 'ConPTY' `
    -or $watchInteractive.chat_provider -ne 'codex' `
    -or -not $watchInteractive.marker_matched `
    -or $watchInteractive.quit_key -ne 'q' `
    -or $watchInteractive.root_exit_code -ne 0 `
    -or -not $watchInteractive.root_created_suspended `
    -or -not $watchInteractive.job_assigned_before_resume `
    -or -not $watchInteractive.root_resumed `
    -or -not $watchInteractive.graceful_cleanup_observed `
    -or $watchInteractive.graceful_owned_processes_remaining -ne 0 `
    -or $watchInteractive.emergency_cleanup_invoked `
    -or $watchInteractive.provider_processes_remaining -ne 0 `
    -or $watchInteractive.dispatcher_processes_remaining -ne 0 `
    -or $watchInteractive.owned_processes_remaining -ne 0
) {
    throw 'Interactive native Windows spec watch proof contradicted a required invariant'
}
Set-EvidenceClaim `
    -Id 'runtime.watch-conpty-chat' `
    -Evidence @(
        'watch-interactive-result.json',
        'watch-interactive-transcript.log',
        'watch-conpty-build.log',
        'watch-conpty-proof.log'
    ) `
    -Detail 'The installed wheel module rendered dashboard, live status, detail, and per-spec chat through ConPTY; its suspended root was assigned to the Job before resume, a real Codex child returned the retained marker, and q drained every exact owned identity before any input, ConPTY, or Job teardown.'

$operatorCodexHome = $env:CODEX_HOME
if (-not $operatorCodexHome) {
    $operatorCodexHome = Join-Path $env:USERPROFILE '.codex'
}
Invoke-LoggedNative -FilePath $sourcePython -Arguments @(
    (Join-Path $sourceRoot 'tools\windows-lab\local_acceptance.py'),
    '--source-root', $sourceRoot,
    '--evidence-root', $evidenceRoot,
    '--fixture-root', $fixtureRoot,
    '--source-revision', $sourceRevision,
    '--native-junit', (Join-Path $evidenceRoot 'native-tests.junit.xml'),
    '--matrix-junit', (Join-Path $evidenceRoot 'installed-cli-matrix.junit.xml'),
    '--wheel-python', $wheelPython,
    '--sdist-python', $sdistPython,
    '--operator-codex-home', $operatorCodexHome
) -LogName 'local-acceptance.log'
Set-EvidenceClaim `
    -Id 'runtime.local-acceptance' `
    -Evidence @(
        'local-acceptance.log',
        'native-command-matrix-result.json',
        'lifecycle-fault-matrix-result.json',
        'isolation-result.json',
        'windows-path-result.json',
        'review-isolation-result.json',
        'native-claude-result.json',
        'update-result.json',
        'test-coverage-result.json',
        'web-action-result.json',
        'watch-result.json',
        'watch-interactive-result.json',
        'watch-interactive-transcript.log',
        'web-integration-result.json',
        'documentation-audit-result.json',
        'package-release-result.json'
    ) `
    -Detail 'Field-level local acceptance artifacts were emitted only after exact native tests, real runtime evidence, and direct Windows path, package, interactive ConPTY watch/chat, documentation, and secret probes passed.'

$result = [ordered]@{
    status = 'evidence-collected'
    acceptance_status = 'requires-fail-closed-audit'
    acceptance_manifest = 'tools/windows-lab/acceptance-manifest.json'
    run_name = $runName
    source_revision = $sourceRevision
    windows_edition = $windowsProduct
    windows_build_number = $windowsBuildNumber
    windows_product_type = $windowsProductType
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
    interactive_conpty_watch_chat = 'passed'
    local_acceptance = 'passed'
    evidence_claims = $evidenceClaims
}
$resultJson = $result | ConvertTo-Json -Depth 12
Write-Utf8NoBom -LiteralPath (Join-Path $evidenceRoot 'result.json') -Value $resultJson
$result | ConvertTo-Json
exit 0
