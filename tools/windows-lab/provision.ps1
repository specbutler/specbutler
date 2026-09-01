$ErrorActionPreference = 'Stop'
$harnessRoot = 'C:\SpecHarness'
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $harnessRoot /grant:r "${account}:(OI)(CI)M" /T /C | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to grant the lab user modify access to the harness workspace' }

$root = Join-Path $harnessRoot 'toolchain'
$manifest = Get-Content -LiteralPath (Join-Path $root 'toolchain.json') -Raw | ConvertFrom-Json

function Get-VerifiedArtifact {
    param([Parameter(Mandatory = $true)] $Entry)
    $path = Join-Path $root $Entry.filename
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing toolchain artifact: $path" }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Entry.sha256.ToLowerInvariant()) {
        throw "SHA-256 mismatch for $($Entry.filename)"
    }
    return $path
}

function Assert-InstallerExitCode {
    param([Parameter(Mandatory = $true)] [System.Diagnostics.Process] $Process)
    if ($Process.ExitCode -notin @(0, 3010)) {
        throw "Installer failed with exit code $($Process.ExitCode)"
    }
}

$gitInstaller = Get-VerifiedArtifact $manifest.git
$ghInstaller = Get-VerifiedArtifact $manifest.gh
$uvArchive = Get-VerifiedArtifact $manifest.uv
$codexArchive = Get-VerifiedArtifact $manifest.codex
$codexHostArchive = Get-VerifiedArtifact $manifest.codex_code_mode_host
$codexSandbox = Get-VerifiedArtifact $manifest.codex_sandbox_setup

Assert-InstallerExitCode (Start-Process -FilePath $gitInstaller -ArgumentList @(
    '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-'
) -Wait -PassThru)
Assert-InstallerExitCode (Start-Process -FilePath msiexec.exe -ArgumentList "/i `"$ghInstaller`" /qn /norestart" -Wait -PassThru)

$toolsRoot = 'C:\Tools'
$uvRoot = Join-Path $toolsRoot 'uv'
$codexRoot = Join-Path $toolsRoot 'Codex'
$extractRoot = Join-Path $root 'expanded'
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $extractRoot
New-Item -ItemType Directory -Force -Path $uvRoot, $codexRoot, $extractRoot | Out-Null
Expand-Archive -LiteralPath $uvArchive -DestinationPath (Join-Path $extractRoot 'uv') -Force
Expand-Archive -LiteralPath $codexArchive -DestinationPath (Join-Path $extractRoot 'codex') -Force
Expand-Archive -LiteralPath $codexHostArchive -DestinationPath (Join-Path $extractRoot 'codex-host') -Force

$uv = Get-ChildItem (Join-Path $extractRoot 'uv') -Recurse -Filter uv.exe | Select-Object -First 1
if (-not $uv) { throw 'uv.exe was not found in the configured archive' }
Copy-Item -LiteralPath $uv.FullName -Destination (Join-Path $uvRoot 'uv.exe') -Force

Get-ChildItem (Join-Path $extractRoot 'codex') -Recurse -Filter *.exe | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $codexRoot $_.Name) -Force
}
Get-ChildItem (Join-Path $extractRoot 'codex-host') -Recurse -Filter *.exe | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $codexRoot $_.Name) -Force
}
Copy-Item -LiteralPath $codexSandbox -Destination (Join-Path $codexRoot 'codex-windows-sandbox-setup.exe') -Force
$codex = Get-ChildItem $codexRoot -Filter 'codex-x86_64-pc-windows-msvc.exe' | Select-Object -First 1
if (-not $codex) { throw 'the Codex executable was not found in the configured archive' }
Copy-Item -LiteralPath $codex.FullName -Destination (Join-Path $codexRoot 'codex.exe') -Force

$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$requiredPaths = @(
    'C:\Program Files\Git\cmd',
    'C:\Program Files\GitHub CLI',
    $uvRoot,
    $codexRoot
)
foreach ($entry in $requiredPaths) {
    if (($machinePath -split ';') -notcontains $entry) { $machinePath = "$machinePath;$entry" }
}
[Environment]::SetEnvironmentVariable('Path', $machinePath, 'Machine')
$env:Path = "$machinePath;$([Environment]::GetEnvironmentVariable('Path', 'User'))"

& 'C:\Program Files\Git\cmd\git.exe' config --system core.longpaths true
if ($LASTEXITCODE -ne 0) { throw 'git long-path configuration failed' }
& 'C:\Program Files\Git\cmd\git.exe' config --global core.autocrlf false
if ($LASTEXITCODE -ne 0) { throw 'git line-ending configuration failed' }
& (Join-Path $uvRoot 'uv.exe') python install 3.12
if ($LASTEXITCODE -ne 0) { throw 'uv Python installation failed' }

git --version
gh --version | Select-Object -First 1
& (Join-Path $uvRoot 'uv.exe') --version
& (Join-Path $codexRoot 'codex.exe') --version
