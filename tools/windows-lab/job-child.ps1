param(
    [Parameter(Mandatory = $true)]
    [string] $ScriptPath
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$global:OutputEncoding = $utf8

try {
    & $ScriptPath
    exit 0
} catch {
    [Console]::Error.WriteLine(($_ | Out-String))
    exit 1
}
