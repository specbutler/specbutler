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

function Assert-OrdinaryFile {
    param([Parameter(Mandatory = $true)] [string] $LiteralPath)
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if ($item.PSIsContainer -or
        (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Expected an ordinary file: $LiteralPath"
    }
    $hardlinks = @(
        & fsutil.exe hardlink list $LiteralPath 2>$null |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Could not verify the file link count: $LiteralPath"
    }
    if ($hardlinks.Count -ne 1) {
        throw "Expected a single-link file: $LiteralPath"
    }
}

function Get-OwnerSidValue {
    param([Parameter(Mandatory = $true)] $Acl)
    try {
        return ([System.Security.Principal.SecurityIdentifier] $Acl.Owner).Value
    } catch {
        return (
            [System.Security.Principal.NTAccount] $Acl.Owner
        ).Translate([System.Security.Principal.SecurityIdentifier]).Value
    }
}

function Assert-TrustedExistingPath {
    param(
        [Parameter(Mandatory = $true)] [string] $LiteralPath,
        [switch] $AllowWriteRepair
    )
    $acl = Get-Acl -LiteralPath $LiteralPath
    $trustedSids = @('S-1-5-18', 'S-1-5-32-544', $identity.User.Value)
    $ownerSid = Get-OwnerSidValue -Acl $acl
    if ($ownerSid -notin $trustedSids) {
        throw "Existing recovery path has an untrusted owner: $LiteralPath"
    }
    if ($AllowWriteRepair) { return }
    $dangerousRights = (
        [System.Security.AccessControl.FileSystemRights]::Write -bor
        [System.Security.AccessControl.FileSystemRights]::Delete -bor
        [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [System.Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $rules = @(
        $acl.GetAccessRules(
            $true,
            $true,
            [System.Security.Principal.SecurityIdentifier]
        )
    )
    foreach ($rule in $rules) {
        if ($rule.AccessControlType -eq
                [System.Security.AccessControl.AccessControlType]::Allow -and
            $rule.IdentityReference.Value -notin $trustedSids -and
            ($rule.FileSystemRights -band $dangerousRights) -ne 0) {
            throw "Existing recovery path grants write access to an untrusted principal: $LiteralPath"
        }
    }
}

function Set-ExactProtectedAcl {
    param(
        [Parameter(Mandatory = $true)] [string] $LiteralPath,
        [Parameter(Mandatory = $true)] [bool] $Container,
        [switch] $IncludeIdentity,
        [switch] $IncludeUsersReadAndExecute
    )
    $systemSidValue = 'S-1-5-18'
    $administratorsSidValue = 'S-1-5-32-544'
    $usersSidValue = 'S-1-5-32-545'
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new($systemSidValue)
    $administratorsSid = [System.Security.Principal.SecurityIdentifier]::new(
        $administratorsSidValue
    )
    $usersSid = [System.Security.Principal.SecurityIdentifier]::new($usersSidValue)
    if ($Container) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $inheritance = (
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    # Windows normalizes an allow-Modify ACE to include Synchronize.
    $modify = (
        [System.Security.AccessControl.FileSystemRights]::Modify -bor
        [System.Security.AccessControl.FileSystemRights]::Synchronize
    )
    $readAndExecute = (
        [System.Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
        [System.Security.AccessControl.FileSystemRights]::Synchronize
    )
    $expectedRules = @(
        [pscustomobject]@{
            Sid = $systemSid
            SidValue = $systemSidValue
            Rights = $fullControl
        },
        [pscustomobject]@{
            Sid = $administratorsSid
            SidValue = $administratorsSidValue
            Rights = $fullControl
        }
    )
    if ($IncludeIdentity) {
        $expectedRules += [pscustomobject]@{
            Sid = $identity.User
            SidValue = $identity.User.Value
            Rights = $modify
        }
    }
    if ($IncludeUsersReadAndExecute) {
        $expectedRules += [pscustomobject]@{
            Sid = $usersSid
            SidValue = $usersSidValue
            Rights = $readAndExecute
        }
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administratorsSid)
    foreach ($expectedRule in $expectedRules) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $expectedRule.Sid,
            $expectedRule.Rights,
            $inheritance,
            $propagation,
            $allow
        )
        [void] $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $LiteralPath -AclObject $acl

    $observed = Get-Acl -LiteralPath $LiteralPath
    if (-not $observed.AreAccessRulesProtected) {
        throw "Protected ACL still inherits access rules: $LiteralPath"
    }
    $ownerSid = Get-OwnerSidValue -Acl $observed
    if ($ownerSid -ne $administratorsSidValue) {
        throw "Protected ACL has an unexpected owner: $LiteralPath"
    }
    $rules = @(
        $observed.GetAccessRules(
            $true,
            $false,
            [System.Security.Principal.SecurityIdentifier]
        )
    )
    if ($rules.Count -ne $expectedRules.Count) {
        throw "Protected ACL has unexpected explicit access rules: $LiteralPath"
    }
    foreach ($expectedRule in $expectedRules) {
        $matching = @(
            $rules | Where-Object {
                $_.IdentityReference.Value -eq $expectedRule.SidValue
            }
        )
        if ($matching.Count -ne 1) {
            throw "Protected ACL is missing its required principal: $LiteralPath"
        }
        $rule = $matching[0]
        if ($rule.AccessControlType -ne $allow -or
            $rule.FileSystemRights -ne $expectedRule.Rights -or
            $rule.InheritanceFlags -ne $inheritance -or
            $rule.PropagationFlags -ne $propagation -or
            $rule.IsInherited) {
            throw "Protected ACL has an unexpected grant: $LiteralPath"
        }
    }
}

function Invoke-DirectDisarm {
    if (Test-Path -LiteralPath $winlogon) {
        Remove-ItemProperty -LiteralPath $winlogon -Name DefaultPassword `
            -ErrorAction SilentlyContinue
        Remove-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount `
            -ErrorAction SilentlyContinue
        New-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon `
            -Value '0' -PropertyType String -Force | Out-Null
    }
    Stop-ScheduledTask -TaskName $cleanupTaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $cleanupTaskName -Confirm:$false `
        -ErrorAction SilentlyContinue
    if ($null -ne (Get-ScheduledTask -TaskName $cleanupTaskName -ErrorAction SilentlyContinue)) {
        throw 'Console recovery cleanup task remained registered.'
    }
    if (Test-Path -LiteralPath $winlogon) {
        $state = Get-ItemProperty -LiteralPath $winlogon
        if ($state.AutoAdminLogon -ne '0' -or
            $null -ne $state.PSObject.Properties['DefaultPassword'] -or
            $null -ne $state.PSObject.Properties['AutoLogonCount']) {
            throw 'Console recovery remained armed after cleanup.'
        }
    }
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
    $harnessRoot = Split-Path -Parent $cleanupSource
    $harnessItem = Get-Item -LiteralPath $harnessRoot -Force
    if (-not $harnessItem.PSIsContainer -or
        (($harnessItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Expected an ordinary directory: $harnessRoot"
    }
    Assert-TrustedExistingPath -LiteralPath $harnessRoot -AllowWriteRepair
    # Codex's elevated native sandbox executes through dedicated local users.
    # They need traversal and read/execute access to the staged toolchain below
    # the harness root, but must never gain write or delete rights here.
    Set-ExactProtectedAcl -LiteralPath $harnessRoot -Container $true `
        -IncludeIdentity -IncludeUsersReadAndExecute
    Assert-OrdinaryFile -LiteralPath $cleanupSource
    Assert-TrustedExistingPath -LiteralPath $cleanupSource -AllowWriteRepair
    Set-ExactProtectedAcl -LiteralPath $cleanupSource -Container $false -IncludeIdentity
    $cleanupBytes = [System.IO.File]::ReadAllBytes($cleanupSource)
    if (Test-Path -LiteralPath $secureRoot) {
        $rootItem = Get-Item -LiteralPath $secureRoot -Force
        if (-not $rootItem.PSIsContainer -or
            (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "Expected an ordinary directory: $secureRoot"
        }
        Assert-TrustedExistingPath -LiteralPath $secureRoot
    } else {
        New-Item -ItemType Directory -Path $secureRoot | Out-Null
    }
    # Older recovery attempts may have left this file with an empty ACL. The
    # directory itself remains intentionally locked to SYSTEM and
    # Administrators, so repair the existing child explicitly before trying to
    # replace it. Applying /inheritance:r recursively is unsafe here: on a
    # child file it can remove the inherited grants without adding effective
    # replacement ACEs.
    if (Test-Path -LiteralPath $cleanupScript) {
        Assert-OrdinaryFile -LiteralPath $cleanupScript
        Assert-TrustedExistingPath -LiteralPath $cleanupScript
        & takeown.exe '/F' $cleanupScript '/A' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to recover ownership of the console cleanup script.'
        }
        & icacls.exe $cleanupScript '/inheritance:r' `
            '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to recover access to the console cleanup script.'
        }
    }
    Set-ExactProtectedAcl -LiteralPath $secureRoot -Container $true
    if (Test-Path -LiteralPath $cleanupScript) {
        Remove-Item -LiteralPath $cleanupScript -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $cleanupScript) {
            throw 'Console cleanup script remained after checked removal.'
        }
    }
    $cleanupStream = [System.IO.File]::Open(
        $cleanupScript,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $cleanupStream.Write($cleanupBytes, 0, $cleanupBytes.Length)
        $cleanupStream.Flush($true)
    } finally {
        $cleanupStream.Dispose()
    }
    Set-ExactProtectedAcl -LiteralPath $cleanupScript -Container $false
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $expectedCleanupHash = [System.BitConverter]::ToString(
            $hasher.ComputeHash($cleanupBytes)
        ).Replace('-', '')
    } finally {
        $hasher.Dispose()
        $cleanupBytes = $null
    }
    $observedCleanupHash = (
        Get-FileHash -LiteralPath $cleanupScript -Algorithm SHA256
    ).Hash
    if ($observedCleanupHash -cne $expectedCleanupHash) {
        throw 'Protected console cleanup script does not match the staged source.'
    }
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
        if (-not (Test-Path -LiteralPath $winlogon)) {
            New-Item -Path $winlogon | Out-Null
        }
        New-ItemProperty -LiteralPath $winlogon -Name DefaultDomainName `
            -Value $domainName -PropertyType String -Force | Out-Null
        New-ItemProperty -LiteralPath $winlogon -Name DefaultUserName `
            -Value $userName -PropertyType String -Force | Out-Null
        New-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon `
            -Value '1' -PropertyType String -Force | Out-Null
        # Defense in depth if the SYSTEM cleanup task is ever unavailable.
        New-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount `
            -Value 1 -PropertyType DWord -Force | Out-Null
        # Write the credential only after every one-shot recovery trigger is
        # armed, minimizing the interval in which a crash could strand it.
        New-ItemProperty -LiteralPath $winlogon -Name DefaultPassword `
            -Value $password -PropertyType String -Force | Out-Null
    } catch {
        Invoke-DirectDisarm
        throw
    } finally {
        $password = $null
    }
    Write-Status -Status 'armed'
    exit 0
}

Invoke-DirectDisarm
Write-Status -Status 'disarmed' -SessionId (Get-InteractiveDesktopSession)
