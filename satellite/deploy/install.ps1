<#
.SYNOPSIS
    Install the MAST satellite on the instrument PC (maintenance window, user present).

.DESCRIPTION
    - Copies MastSatellite.exe (+ .exe.config) to a fixed folder (default C:\MAST-Satellite).
    - Creates the state folder (default %ProgramData%\MAST-Satellite) and gives the logon user
      Modify rights on it (journal, logs, upload manifest, token live there).
    - Copies the config template to <state>\satellite.json ONLY if no config exists yet.
    - Registers a scheduled task that starts the satellite when that user logs on,
      "run only when user is logged on" (interactive desktop session: LightField Automation
      and the status window need it). Nothing is started unless -StartNow is given.

    Run from an elevated Windows PowerShell 5.1 prompt in the satellite folder (site values such
    as the logon user and the config file are parameters; see satellite/README.md):
        powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1 -UserName <logon user> `
            -SourceDir <folder with MastSatellite.exe> -ConfigTemplate .\config\<site>.satellite.json
    Then, as the logon user (not elevated): deploy\set-token.ps1, fill hub.spki_sha256,
    and MastSatellite.exe --check-config.

    This script is ASCII only on purpose (Chinese Windows reads non-ASCII .ps1 files as GBK).
#>
[CmdletBinding()]
param(
    [string]$SourceDir = (Join-Path $PSScriptRoot '..\MastSatellite\bin\x64\Release'),
    [Parameter(Mandatory = $true)][string]$ConfigTemplate,
    [string]$InstallDir = 'C:\MAST-Satellite',
    [string]$StateDir = (Join-Path $env:ProgramData 'MAST-Satellite'),
    [Parameter(Mandatory = $true)][string]$UserName,
    [string]$TaskName = 'MAST Satellite',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'

# MastSatellite.exe is a GUI-subsystem program: PowerShell does not wait for it unless told to.
function Invoke-Satellite([string]$Exe, [string]$Arguments) {
    $p = Start-Process -FilePath $Exe -ArgumentList $Arguments -Wait -PassThru -NoNewWindow
    return $p.ExitCode
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    throw 'Run this script from an elevated PowerShell (Run as administrator).'
}

$exeSrc = Join-Path $SourceDir 'MastSatellite.exe'
$cfgSrc = Join-Path $SourceDir 'MastSatellite.exe.config'
if (-not (Test-Path -LiteralPath $exeSrc)) { throw "MastSatellite.exe not found in $SourceDir (build it first, see satellite/README.md)" }
if (-not (Test-Path -LiteralPath $cfgSrc)) { throw "MastSatellite.exe.config not found in $SourceDir" }

$account = "$env:COMPUTERNAME\$UserName"
try {
    $null = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier])
} catch {
    throw "User $account does not exist on this machine"
}

# 1. Stop a running satellite gracefully before replacing the exe.
$exeDst = Join-Path $InstallDir 'MastSatellite.exe'
$cfgDst = Join-Path $StateDir 'satellite.json'
if ((Test-Path -LiteralPath $exeDst) -and (Test-Path -LiteralPath $cfgDst)) {
    Write-Host 'Stopping a running satellite (if any) ...'
    $rc = Invoke-Satellite $exeDst ('--stop --config "' + $cfgDst + '" --timeout 60')
    if ($rc -eq 1) { throw 'The running satellite did not stop within 60 s; close it from its tray icon and run install.ps1 again.' }
}

# 2. Program files.
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -LiteralPath $exeSrc -Destination $InstallDir -Force
Copy-Item -LiteralPath $cfgSrc -Destination $InstallDir -Force
Write-Host "Installed program to $InstallDir"

# 3. State folder: the logon user needs to write journal, logs and manifest.
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
& icacls.exe $StateDir /grant "${account}:(OI)(CI)M" | Out-Null
Write-Host "State folder $StateDir (Modify for $account)"

# 4. Config: never overwrite an existing one.
if (Test-Path -LiteralPath $cfgDst) {
    Write-Host "Keeping existing config $cfgDst"
} else {
    if (-not (Test-Path -LiteralPath $ConfigTemplate)) { throw "Config template not found: $ConfigTemplate" }
    Copy-Item -LiteralPath $ConfigTemplate -Destination $cfgDst
    Write-Host "Copied config template to $cfgDst (fill hub.spki_sha256 before the first start)"
}

# 5. Scheduled task: at logon of $UserName, interactive (only when the user is logged on).
$action = New-ScheduledTaskAction -Execute $exeDst -Argument ('--config "' + $cfgDst + '"') -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'MAST satellite: hosts LightField and the optics devices for the MAST hub (starts at logon of the lab user).' -Force | Out-Null
Write-Host "Registered scheduled task '$TaskName' (at logon of $account, interactive, not elevated)"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started task '$TaskName' (it runs in $UserName's desktop session)"
}

Write-Host ''
Write-Host 'Next steps (as the logon user, not elevated):'
Write-Host "  1. powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\set-token.ps1`""
Write-Host "  2. put the hub pin into $cfgDst  (hub.spki_sha256, 64 hex)"
Write-Host "  3. `"$exeDst`" --check-config --config `"$cfgDst`""
Write-Host "  4. Start-ScheduledTask -TaskName '$TaskName'   (or log off / on)"
