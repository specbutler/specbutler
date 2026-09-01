param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,47}$')]
    [string] $JobName
)

$ErrorActionPreference = 'Stop'
$jobRoot = 'C:\SpecHarness\jobs'
$script = Join-Path $jobRoot "$JobName.ps1"
$log = Join-Path $jobRoot "$JobName.log"
$done = Join-Path $jobRoot "$JobName.done.json"
Remove-Item -LiteralPath $log, $done -Force -ErrorAction SilentlyContinue
$result = [ordered]@{
    job = $JobName
    status = 'failed'
    exit_code = 1
    session_id = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    started_at = (Get-Date).ToString('o')
    finished_at = $null
}

try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script *>&1 |
        Tee-Object -LiteralPath $log
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
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
