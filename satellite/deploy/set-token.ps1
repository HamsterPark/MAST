<#
.SYNOPSIS
    Store the hub token for the MAST satellite, DPAPI-protected.

.DESCRIPTION
    The token comes from the hub PC, printed once by the installed MAST (see satellite/README.md):
        cmd /c 'C:\MAST\MAST.exe --satellite-admin new-token <satellite id>'
    Type or paste it here; it is never echoed or written in clear.

    Run AS THE LOGON USER (the one install.ps1 registered) in that user's desktop session, not elevated:
        powershell -ExecutionPolicy Bypass -File .\deploy\set-token.ps1
    DPAPI CurrentUser scope ties the file to that Windows account. Over SSH with key login
    DPAPI may be unavailable; then run it at the console, or use -Scope LocalMachine
    (any process on this PC could then decrypt it; the file ACL is still restricted).

    Blob format (must match MastSatellite TokenStore): ProtectedData.Protect(UTF8(token),
    ASCII("MAST-Satellite-token-v1"), scope). ASCII-only script (Chinese Windows reads
    non-ASCII .ps1 files as GBK).
#>
[CmdletBinding()]
param(
    [string]$TokenFile = (Join-Path $env:ProgramData 'MAST-Satellite\token.dpapi'),
    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$Scope = 'CurrentUser',
    # Read the token from standard input instead of prompting (for scripted setup).
    [switch]$FromStdin
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

if ($FromStdin) {
    $token = [Console]::In.ReadLine()
    if ($null -eq $token) { $token = '' }
    $token = $token.Trim()
} else {
    $secure = Read-Host -AsSecureString -Prompt 'Satellite token (from the hub admin tool)'
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
if ($token.Length -lt 16) { throw 'Token is empty or too short.' }
if ($token -match '\s') { throw 'Token must not contain spaces or line breaks.' }

$entropy = [Text.Encoding]::ASCII.GetBytes('MAST-Satellite-token-v1')
$plain = [Text.Encoding]::UTF8.GetBytes($token)
$scopeValue = [Security.Cryptography.DataProtectionScope]::$Scope
$blob = [Security.Cryptography.ProtectedData]::Protect($plain, $entropy, $scopeValue)
[Array]::Clear($plain, 0, $plain.Length)
$token = $null

$dir = Split-Path -Parent $TokenFile
if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
[IO.File]::WriteAllBytes($TokenFile, $blob)

# Only this user, SYSTEM and Administrators may read the blob.
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $TokenFile /inheritance:r /grant:r "${me}:(R,W)" 'SYSTEM:(F)' '*S-1-5-32-544:(F)' | Out-Null

# Read it back to prove the satellite (same user) can decrypt it.
$check = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($TokenFile), $entropy, $scopeValue)
if ($check.Length -lt 16) { throw 'Read-back failed.' }
[Array]::Clear($check, 0, $check.Length)

Write-Host "Token stored in $TokenFile ($Scope scope, readable by $me)."
Write-Host 'Restart the satellite (or log off / on) to use it.'
