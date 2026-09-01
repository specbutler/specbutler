param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,47}$')]
    [string] $JobName
)

$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$arguments = "-NoProfile -ExecutionPolicy Bypass -File C:\SpecHarness\job-runner.ps1 -JobName $JobName"
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
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Started $JobName in the logged-on Windows console session."
