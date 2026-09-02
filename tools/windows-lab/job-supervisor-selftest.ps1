param(
    [Parameter(Mandatory = $true)]
    [string] $SourceRoot,

    [Parameter(Mandatory = $true)]
    [string] $OutputRoot,

    [string] $PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceRoot).Path
$output = [System.IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$resultPath = Join-Path $output 'job-supervisor-selftest.json'
$result = [ordered]@{
    status = 'failed'
    powershell_edition = $PSVersionTable.PSEdition
    powershell_version = $PSVersionTable.PSVersion.ToString()
    python_executable = ''
    argv_fidelity = $false
    exit_propagation = $false
    timeout_tree_cleanup = $false
    lingering_tree_cleanup = $false
    utf8_stdout_stderr = $false
    redaction = $false
    error = $null
}
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'specbutler-supervisor-selftest-' + [Guid]::NewGuid().ToString('N')
)

function Assert-Probe {
    param(
        [Parameter(Mandatory = $true)]
        [bool] $Condition,

        [Parameter(Mandatory = $true)]
        [string] $Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Wait-ProcessIdentityGone {
    param(
        [Parameter(Mandatory = $true)]
        [int[]] $ProcessId,

        [int] $TimeoutSeconds = 10
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $remaining = @(
            $ProcessId | Where-Object {
                $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
            }
        )
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

try {
    Assert-Probe (
        $PSVersionTable.PSEdition -eq 'Desktop' -and
        $PSVersionTable.PSVersion.Major -eq 5
    ) 'Supervisor probe must run under Windows PowerShell 5.1.'
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

    $parseErrors = @()
    foreach ($name in @('job-child.ps1', 'job-runner.ps1')) {
        $tokens = $null
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $source "tools\windows-lab\$name"),
            [ref]$tokens,
            [ref]$errors
        ) | Out-Null
        $parseErrors += @($errors)
    }
    Assert-Probe ($parseErrors.Count -eq 0) 'Windows PowerShell parsing failed.'
    Add-Type -Path (Join-Path $source 'tools\windows-lab\job-supervisor.cs')

    $pythonCandidates = @()
    if ($PythonExe) {
        $pythonCandidates += $PythonExe
    }
    $pythonCandidates += (Join-Path $source '.venv\Scripts\python.exe')
    $pythonCandidates += @(
        Get-Command python.exe -All -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandType -eq 'Application' } |
            ForEach-Object { $_.Source }
    )
    $python = @(
        $pythonCandidates |
            Where-Object {
                $_ -and
                $_ -notmatch '(?i)\\WindowsApps\\' -and
                (Test-Path -LiteralPath $_ -PathType Leaf)
            } |
            Select-Object -Unique
    ) | Select-Object -First 1
    Assert-Probe ([bool]$python) 'No real Python interpreter was available for the probe.'
    $python = (Resolve-Path -LiteralPath $python).Path
    $result.python_executable = $python
    $helper = Join-Path $tempRoot 'probe-helper.py'
    $helperSource = @'
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode = sys.argv[1]
if mode == "argv":
    Path(sys.argv[2]).write_text(
        json.dumps({"arguments": sys.argv[3:]}, ensure_ascii=False),
        encoding="utf-8",
    )
    raise SystemExit(7)
if mode == "child":
    time.sleep(300)
    raise SystemExit(0)
if mode in {"timeout-tree", "lingering-tree"}:
    child = subprocess.Popen([sys.executable, __file__, "child"])
    Path(sys.argv[2]).write_text(
        json.dumps({"parent": os.getpid(), "child": child.pid}),
        encoding="ascii",
    )
    if mode == "timeout-tree":
        time.sleep(300)
    raise SystemExit(0)
raise SystemExit("unknown probe mode")
'@
    [System.IO.File]::WriteAllText($helper, $helperSource, $utf8)

    $snow = [string][char]0x96EA
    $argvResult = Join-Path $tempRoot 'argv.json'
    $argvLog = Join-Path $tempRoot 'argv.log'
    $expectedArguments = [string[]]@(
        'plain',
        'space value',
        $snow,
        'quote"value',
        '',
        'C:\trail\',
        'C:\double\\'
    )
    $supervised = [SpecButlerLabJobSupervisor]::Run(
        $python,
        [string[]]@($helper, 'argv', $argvResult) + $expectedArguments,
        $tempRoot,
        $argvLog,
        30000
    )
    Assert-Probe ($supervised.ExitCode -eq 7) 'Exit code 7 was not propagated.'
    Assert-Probe (-not $supervised.TimedOut) 'The argv probe unexpectedly timed out.'
    Assert-Probe $supervised.DescendantsGone 'The argv probe retained descendants.'
    $argvDocument = Get-Content -LiteralPath $argvResult -Raw -Encoding UTF8 |
        ConvertFrom-Json
    $actualArguments = @($argvDocument.arguments)
    Assert-Probe (
        $actualArguments.Count -eq $expectedArguments.Count
    ) 'The argv probe changed the argument count.'
    for ($index = 0; $index -lt $expectedArguments.Count; $index++) {
        Assert-Probe (
            $actualArguments[$index] -ceq $expectedArguments[$index]
        ) "The argv probe changed argument $index."
    }
    $result.argv_fidelity = $true
    $result.exit_propagation = $true

    $timeoutPids = Join-Path $tempRoot 'timeout-pids.json'
    $timeoutLog = Join-Path $tempRoot 'timeout.log'
    $supervised = [SpecButlerLabJobSupervisor]::Run(
        $python,
        [string[]]@($helper, 'timeout-tree', $timeoutPids),
        $tempRoot,
        $timeoutLog,
        3000
    )
    $timeoutTree = Get-Content -LiteralPath $timeoutPids -Raw | ConvertFrom-Json
    Assert-Probe $supervised.TimedOut 'The timeout probe did not report a timeout.'
    Assert-Probe ($supervised.ExitCode -eq 124) 'The timeout probe did not return 124.'
    Assert-Probe $supervised.DescendantsGone 'The timeout probe retained descendants.'
    Assert-Probe (
        Wait-ProcessIdentityGone @([int]$timeoutTree.parent, [int]$timeoutTree.child)
    ) 'The timeout probe left its Python process tree alive.'
    $result.timeout_tree_cleanup = $true

    $lingeringPids = Join-Path $tempRoot 'lingering-pids.json'
    $lingeringLog = Join-Path $tempRoot 'lingering.log'
    $lingeringDetected = $false
    try {
        [SpecButlerLabJobSupervisor]::Run(
            $python,
            [string[]]@($helper, 'lingering-tree', $lingeringPids),
            $tempRoot,
            $lingeringLog,
            30000
        ) | Out-Null
    } catch {
        $exceptionMessages = @()
        $currentException = $_.Exception
        while ($null -ne $currentException) {
            $exceptionMessages += ($currentException.Message -replace '\s+', ' ')
            $currentException = $currentException.InnerException
        }
        if (
            $exceptionMessages -match
            'root exited while descendant processes remained'
        ) {
            $lingeringDetected = $true
        } else {
            throw
        }
    }
    $lingeringTree = Get-Content -LiteralPath $lingeringPids -Raw | ConvertFrom-Json
    Assert-Probe $lingeringDetected 'A lingering child was not detected.'
    Assert-Probe (
        Wait-ProcessIdentityGone @([int]$lingeringTree.parent, [int]$lingeringTree.child)
    ) 'The detected lingering process tree remained alive.'
    $result.lingering_tree_cleanup = $true

    $fakeToken = 'github_pat_' + ('A' * 82)
    $outputScript = Join-Path $tempRoot 'utf8-output.ps1'
    $outputSource = @"
`$snow = [string][char]0x96EA
[Console]::Out.WriteLine("stdout=`$snow $fakeToken")
[Console]::Error.WriteLine("stderr=`$snow $fakeToken")
"@
    [System.IO.File]::WriteAllText($outputScript, $outputSource, $utf8)
    $utf8Log = Join-Path $tempRoot 'utf8.log'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $supervised = [SpecButlerLabJobSupervisor]::Run(
        $powershell,
        [string[]]@(
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            (Join-Path $source 'tools\windows-lab\job-child.ps1'),
            '-ScriptPath',
            $outputScript
        ),
        $tempRoot,
        $utf8Log,
        30000
    )
    Assert-Probe ($supervised.ExitCode -eq 0) 'The UTF-8 wrapper probe failed.'
    $utf8Text = $strictUtf8.GetString([System.IO.File]::ReadAllBytes($utf8Log))
    Assert-Probe ($utf8Text -match "stdout=$snow") 'UTF-8 stdout was not preserved.'
    Assert-Probe ($utf8Text -match "stderr=$snow") 'UTF-8 stderr was not preserved.'
    $result.utf8_stdout_stderr = $true

    $raw = Join-Path $tempRoot 'raw'
    $sanitized = Join-Path $output 'sanitized'
    New-Item -ItemType Directory -Force -Path $raw | Out-Null
    Copy-Item -LiteralPath $utf8Log -Destination (Join-Path $raw 'job.log')
    & $python (Join-Path $source 'tools\windows-lab\redact.py') $raw $sanitized
    Assert-Probe ($LASTEXITCODE -eq 0) 'The current redactor rejected UTF-8 output.'
    $sanitizedText = $strictUtf8.GetString(
        [System.IO.File]::ReadAllBytes((Join-Path $sanitized 'job.log'))
    )
    Assert-Probe (-not $sanitizedText.Contains($fakeToken)) 'The fake token remained after redaction.'
    Assert-Probe ($sanitizedText.Contains('[REDACTED]')) 'No redaction marker was emitted.'
    Assert-Probe ($sanitizedText.Contains($snow)) 'Redaction corrupted Unicode output.'
    $redactionReport = Get-Content -LiteralPath (
        Join-Path $sanitized '_redaction-report.json'
    ) -Raw | ConvertFrom-Json
    Assert-Probe ($redactionReport.status -eq 'passed') 'Redaction report did not pass.'
    Assert-Probe (
        @($redactionReport.undecodable_text_files).Count -eq 0
    ) 'Redaction reported undecodable text.'
    $result.redaction = $true
    $result.status = 'passed'
} catch {
    $result.error = ($_ | Out-String).Trim()
} finally {
    [System.IO.File]::WriteAllText(
        $resultPath,
        (($result | ConvertTo-Json -Depth 5) + [Environment]::NewLine),
        $utf8
    )
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($result.status -ne 'passed') {
    throw "Windows supervisor self-test failed: $($result.error)"
}
