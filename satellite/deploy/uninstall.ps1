<#
.SYNOPSIS
    Remove the MAST satellite from the instrument PC.

.DESCRIPTION
    Stops a running satellite gracefully (MastSatellite.exe --stop: running commands end,
    devices are released, the hub connection is closed with code 1000), removes the
    scheduled task and the program folder. The state folder (config, token, journal,
    logs, upload manifest) is kept unless -RemoveState is given. The watched data folder is never touched.

    Run from an elevated Windows PowerShell 5.1 prompt:
        powershell -ExecutionPolicy Bypass -File .\deploy\uninstall.ps1 [-RemoveState]
    ASCII-only script (Chinese Windows reads non-ASCII .ps1 files as GBK).
#>
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\MAST-Satellite',
    [string]$StateDir = (Join-Path $env:ProgramData 'MAST-Satellite'),
    [string]$TaskName = 'MAST Satellite',
    [switch]$RemoveState
)

$ErrorActionPreference = 'Stop'

$exe = Join-Path $InstallDir 'MastSatellite.exe'
$cfg = Join-Path $StateDir 'satellite.json'
if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $cfg)) {
    Write-Host 'Stopping the satellite gracefully ...'
    # GUI-subsystem exe: wait for it explicitly.
    $p = Start-Process -FilePath $exe -ArgumentList ('--stop --config "' + $cfg + '" --timeout 60') -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -eq 1) {
        Write-Warning 'The satellite did not stop within 60 s. Close it from its tray icon, then run this script again.'
        exit 1
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'"
}

if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Host "Removed $InstallDir"
}

if ($RemoveState) {
    if (Test-Path -LiteralPath $StateDir) {
        Remove-Item -LiteralPath $StateDir -Recurse -Force
        Write-Host "Removed $StateDir (config, token, journal, logs, manifest)"
    }
} else {
    Write-Host "Kept $StateDir (use -RemoveState to delete config, token, journal and logs)"
}
