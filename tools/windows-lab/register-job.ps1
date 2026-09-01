param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,47}$')]
    [string] $JobName
)

$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$arguments = "-NoProfile -ExecutionPolicy Bypass -File C:\SpecHarness\job-runner.ps1 -JobName $JobName"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$taskName = "SpecButlerLab-$JobName"
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Started $JobName in the logged-on Windows console session."
