param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,47}$')]
    [string] $JobName,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{32}$')]
    [string] $LaunchNonce
)

$ErrorActionPreference = 'Stop'
$jobRoot = 'C:\SpecHarness\jobs'
$script = Join-Path $jobRoot "$JobName.ps1"
$log = Join-Path $jobRoot "$JobName.log"
$done = Join-Path $jobRoot "$JobName.done.json"
$started = Join-Path $jobRoot "$JobName.started.json"
$startedTemp = Join-Path $jobRoot "$JobName.started.json.tmp"
$release = Join-Path $jobRoot "$JobName.release"
$temp = Join-Path $jobRoot "$JobName.temp"
Remove-Item -LiteralPath $log, $done -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $temp | Out-Null
$env:TEMP = $temp
$env:TMP = $temp
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$startedResult = [ordered]@{
    job = $JobName
    launch_nonce = $LaunchNonce
    user_name = $identity.Name
    user_sid = $identity.User.Value
    session_id = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    started_at = (Get-Date).ToString('o')
}
$startedJson = $startedResult | ConvertTo-Json
[System.IO.File]::WriteAllText($startedTemp, $startedJson, [System.Text.Encoding]::ASCII)
Move-Item -LiteralPath $startedTemp -Destination $started -Force
$result = [ordered]@{
    job = $JobName
    status = 'failed'
    exit_code = 1
    session_id = $startedResult.session_id
    started_at = (Get-Date).ToString('o')
    finished_at = $null
}

try {
    $releaseDeadline = [DateTime]::UtcNow.AddSeconds(120)
    while (-not (Test-Path -LiteralPath $release) -and
        [DateTime]::UtcNow -lt $releaseDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not (Test-Path -LiteralPath $release)) {
        throw 'The host did not release the acknowledged interactive job within 120 seconds.'
    }
    $releasedNonce = (Get-Content -LiteralPath $release -Raw).Trim()
    Remove-Item -LiteralPath $release -Force -ErrorAction SilentlyContinue
    if ($releasedNonce -cne $LaunchNonce) {
        throw 'The interactive job release nonce did not match its launch receipt.'
    }
    # Windows PowerShell 5 wraps a child process's stderr as NativeCommandError.
    # Git, pytest, and providers legitimately use stderr on successful runs, so
    # preserve the merged live log but determine success from the exit code.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script *>&1 |
            Tee-Object -LiteralPath $log
        $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { throw "Job exited with code $exitCode" }
    $result.status = 'ok'
    $result.exit_code = 0
} catch {
    $_ | Out-String | Add-Content -LiteralPath $log
} finally {
    $result.finished_at = (Get-Date).ToString('o')
    $result | ConvertTo-Json | Set-Content -LiteralPath $done -Encoding ascii
}
exit $result.exit_code
