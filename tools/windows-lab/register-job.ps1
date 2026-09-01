param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,47}$')]
    [string] $JobName,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{32}$')]
    [string] $LaunchNonce
)

$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$desktopSessions = @(
    Get-Process -Name explorer -IncludeUserName -ErrorAction SilentlyContinue |
        Where-Object {
            $_.SessionId -gt 0 -and $_.UserName -ieq $identity.Name
        } |
        Select-Object -ExpandProperty SessionId -Unique
)
if ($desktopSessions.Count -eq 0) {
    throw "No Explorer desktop session is active for $($identity.Name); refusing to queue an interactive proof."
}

$jobRoot = 'C:\SpecHarness\jobs'
$started = Join-Path $jobRoot "$JobName.started.json"
$release = Join-Path $jobRoot "$JobName.release"
Remove-Item -LiteralPath $started, "$started.tmp", $release, "$release.tmp" `
    -Force -ErrorAction SilentlyContinue
$arguments = "-NoProfile -ExecutionPolicy Bypass -File C:\SpecHarness\job-runner.ps1 -JobName $JobName -LaunchNonce $LaunchNonce"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
# Product proof must match the documented user tier.  The SSH control plane
# provisions the machine with administrative rights, but interactive jobs run
# with the logged-on account's filtered, non-elevated token.
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$taskName = "SpecButlerLab-$JobName"
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $receipt = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $started) {
            try {
                $receipt = Get-Content -LiteralPath $started -Raw | ConvertFrom-Json
            } catch {
                $receipt = $null
            }
            if ($null -ne $receipt) { break }
        }
        Start-Sleep -Milliseconds 250
    }
    if ($null -eq $receipt) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
        throw "Interactive runner did not acknowledge launch within 30 seconds (state=$($task.State), last_result=$($info.LastTaskResult))."
    }
    if ($receipt.job -ne $JobName -or
        $receipt.launch_nonce -cne $LaunchNonce -or
        $receipt.user_name -ine $identity.Name -or
        $receipt.user_sid -ne $identity.User.Value -or
        [int]$receipt.session_id -notin $desktopSessions) {
        throw 'Interactive runner produced a launch receipt for the wrong job, nonce, user, or desktop session.'
    }
    [ordered]@{
        status = 'confirmed'
        job = $JobName
        launch_nonce = $LaunchNonce
        user_name = $receipt.user_name
        user_sid = $receipt.user_sid
        session_id = [int]$receipt.session_id
    } | ConvertTo-Json -Compress
} catch {
    $launchFailure = $_
    try {
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -ne $existingTask) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        }
        if ($null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
            throw 'interactive task remained registered after launch failure'
        }
    } catch {
        throw "Interactive launch failed and task cleanup could not be verified: $launchFailure; cleanup: $_"
    }
    throw $launchFailure
}
