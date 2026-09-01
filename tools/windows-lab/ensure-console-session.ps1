param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Check', 'Arm', 'Disarm')]
    [string] $Mode
)

$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$cleanupTaskName = 'SpecButlerLab-DisarmAutologon'
$secureRoot = Join-Path $env:ProgramData 'SpecButlerLab'
$cleanupSource = 'C:\SpecHarness\disarm-autologon.ps1'
$cleanupScript = Join-Path $secureRoot 'disarm-autologon.ps1'

function Get-InteractiveDesktopSession {
    $sessions = @(
        Get-Process -Name explorer -IncludeUserName -ErrorAction SilentlyContinue |
            Where-Object {
                $_.SessionId -gt 0 -and $_.UserName -ieq $identity.Name
            } |
            Select-Object -ExpandProperty SessionId -Unique
    )
    if ($sessions.Count -eq 0) { return $null }
    return [int]($sessions | Sort-Object | Select-Object -First 1)
}

function Write-Status {
    param(
        [Parameter(Mandatory = $true)] [string] $Status,
        [AllowNull()] [Nullable[int]] $SessionId = $null
    )
    [ordered]@{
        status = $Status
        user_name = $identity.Name
        user_sid = $identity.User.Value
        session_id = $SessionId
    } | ConvertTo-Json -Compress
}

if ($Mode -eq 'Check') {
    $sessionId = Get-InteractiveDesktopSession
    if ($null -eq $sessionId) {
        Write-Error "No Explorer desktop session is active for $($identity.Name)."
        exit 2
    }
    Write-Status -Status 'ready' -SessionId $sessionId
    exit 0
}

if ($Mode -eq 'Arm') {
    # The password arrives only on standard input from the controller's private
    # state file.  It is never passed in argv, written to a guest file, or
    # emitted.  Winlogon necessarily stores it temporarily in the registry.
    $password = [Console]::In.ReadToEnd().TrimEnd([char[]]"`r`n")
    if ([string]::IsNullOrEmpty($password)) {
        throw 'The console recovery password was empty.'
    }
    $separator = $identity.Name.IndexOf('\')
    if ($separator -le 0 -or $separator -ge ($identity.Name.Length - 1)) {
        throw "Unexpected Windows identity: $($identity.Name)"
    }
    $domainName = $identity.Name.Substring(0, $separator)
    $userName = $identity.Name.Substring($separator + 1)
    if (-not (Test-Path -LiteralPath $cleanupSource)) {
        throw "Console cleanup script is missing: $cleanupSource"
    }
    New-Item -ItemType Directory -Path $secureRoot -Force | Out-Null
    Copy-Item -LiteralPath $cleanupSource -Destination $cleanupScript -Force
    & icacls.exe $secureRoot '/inheritance:r' `
        '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' `
        '/T' '/C' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to protect the console cleanup script.' }
    $cleanupAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
        "-NoProfile -ExecutionPolicy Bypass -File $cleanupScript"
    )
    $cleanupTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $cleanupPrincipal = New-ScheduledTaskPrincipal `
        -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $cleanupSettings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Register-ScheduledTask `
        -TaskName $cleanupTaskName `
        -Action $cleanupAction `
        -Trigger $cleanupTrigger `
        -Principal $cleanupPrincipal `
        -Settings $cleanupSettings `
        -Force | Out-Null
    try {
        New-Item -Path $winlogon -Force | Out-Null
        New-ItemProperty -LiteralPath $winlogon -Name DefaultDomainName `
            -Value $domainName -PropertyType String -Force | Out-Null
        New-ItemProperty -LiteralPath $winlogon -Name DefaultUserName `
            -Value $userName -PropertyType String -Force | Out-Null
        New-ItemProperty -LiteralPath $winlogon -Name DefaultPassword `
            -Value $password -PropertyType String -Force | Out-Null
        New-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon `
            -Value '1' -PropertyType String -Force | Out-Null
        # Defense in depth if the SYSTEM cleanup task is ever unavailable.
        New-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount `
            -Value 1 -PropertyType DWord -Force | Out-Null
    } catch {
        & $cleanupScript -RemoveTask
        throw
    } finally {
        $password = $null
    }
    Write-Status -Status 'armed'
    exit 0
}

if (Test-Path -LiteralPath $cleanupScript) {
    & $cleanupScript -RemoveTask
} else {
    Remove-ItemProperty -LiteralPath $winlogon -Name DefaultPassword `
        -ErrorAction SilentlyContinue
    Remove-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount `
        -ErrorAction SilentlyContinue
    New-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon `
        -Value '0' -PropertyType String -Force | Out-Null
}
$state = Get-ItemProperty -LiteralPath $winlogon
if ($state.AutoAdminLogon -ne '0' -or
    $null -ne $state.PSObject.Properties['DefaultPassword'] -or
    $null -ne $state.PSObject.Properties['AutoLogonCount']) {
    throw 'Console recovery remained armed after cleanup.'
}
Write-Status -Status 'disarmed' -SessionId (Get-InteractiveDesktopSession)
