param(
    [switch] $RemoveTask
)

$ErrorActionPreference = 'Stop'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$taskName = 'SpecButlerLab-DisarmAutologon'
$stateRoot = Join-Path $env:ProgramData 'SpecButlerLab'
$marker = Join-Path $stateRoot 'autologon-disarmed.json'
$markerTemp = "$marker.$([Guid]::NewGuid().ToString('N')).tmp"

Remove-ItemProperty -LiteralPath $winlogon -Name DefaultPassword `
    -ErrorAction SilentlyContinue
Remove-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount `
    -ErrorAction SilentlyContinue
New-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon `
    -Value '0' -PropertyType String -Force | Out-Null
$state = Get-ItemProperty -LiteralPath $winlogon
if ($state.AutoAdminLogon -ne '0' -or
    $null -ne $state.PSObject.Properties['DefaultPassword'] -or
    $null -ne $state.PSObject.Properties['AutoLogonCount']) {
    throw 'Failed to remove the temporary console recovery credential.'
}

New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
$result = [ordered]@{
    status = 'disarmed'
    disarmed_at = (Get-Date).ToUniversalTime().ToString('o')
}
[System.IO.File]::WriteAllText(
    $markerTemp,
    ($result | ConvertTo-Json),
    [System.Text.Encoding]::ASCII
)
Move-Item -LiteralPath $markerTemp -Destination $marker -Force

if ($RemoveTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false `
        -ErrorAction SilentlyContinue
    if ($null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        throw 'Failed to remove the console recovery cleanup task.'
    }
}
