<#
.SYNOPSIS
  Turnkey: let an AI (Claude Code / Codex) on another computer control THIS
  Windows PC over your Tailscale network, using the built-in OpenSSH + a key.
  No extra program to keep running: Windows' own SSH service is the "daemon".

.DESCRIPTION
  Run once, from an elevated PowerShell, on the computer you want controlled.
  It:
    1. shows a popup explaining what this turns on, and stops unless you agree;
    2. installs and starts the built-in OpenSSH server (if not already there);
    3. makes a dedicated key for this access and authorizes it on this PC
       (handling the Windows administrators_authorized_keys rule correctly);
    4. limits SSH to your Tailscale network (100.64.0.0/10) unless -AllowLan;
    5. prints the one line to paste into your AI, and saves the private key file
       for you to carry over.

  The window stays open at the end (press Enter to close) so you can read the
  connection line, the key location, or any error.

  Undo: run this script with -Uninstall (the launcher has a one-click button),
  or delete the line it added to the authorized_keys file it names, or
  Stop-Service sshd; Set-Service sshd -StartupType Manual to turn SSH off.

  After a successful install it also writes %USERPROFILE%\.ssh\ai-remote.handoff.json
  so the MAST launcher can keep showing the two Claude Code / Codex lines.

  ASCII only on purpose: Chinese Windows reads non-ASCII .ps1 files as GBK.

  NOTE (verify on your own machine): the
  OpenSSH capability name, the administrators_authorized_keys ACL, the firewall
  rule name OpenSSH-Server-In-TCP, and ssh-keygen's empty-passphrase quoting can
  differ between Windows builds and PowerShell 5.1 vs 7.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup-ai-ssh.ps1
#>
[CmdletBinding()]
param(
    [string]$User = $env:USERNAME,
    [int]$Port = 22,
    [string]$KeyName = 'ai-remote',
    [string]$PublicKey,   # authorize THIS public key (operator-generated) instead of making one
    [switch]$AllowLan,    # do NOT restrict SSH to Tailscale addresses
    [switch]$IAgree,      # skip the popup (for an unattended install); also skips the pause at the end
    [switch]$Uninstall    # revoke the key, stop sshd, remove handoff file (one-click undo)
)

$ErrorActionPreference = 'Stop'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AiRemoteAuthFile {
    param([string]$UserName, [string]$SshDir)
    $adminMember = $false
    try {
        $members = Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object { $_.Name }
        $adminMember = ($members -contains "$env:COMPUTERNAME\$UserName") -or ($members -contains "AzureAD\$UserName")
    } catch { $adminMember = $false }
    if ($adminMember) {
        return (Join-Path $env:ProgramData 'ssh\administrators_authorized_keys')
    }
    return (Join-Path $SshDir 'authorized_keys')
}

try {
    if (-not (Test-Admin)) {
        throw 'Run this from an elevated PowerShell (right-click -> Run as administrator).'
    }

    $sshDir = Join-Path $env:USERPROFILE '.ssh'
    $privPath = Join-Path $sshDir $KeyName
    $handoffPath = Join-Path $sshDir 'ai-remote.handoff.json'

    # --- Uninstall path (one-click undo from the launcher) -----------
    if ($Uninstall) {
        if (-not $IAgree) {
            $msg = @"
Remove AI remote access from THIS computer?

This will:
  - delete the '$KeyName' key line from the authorized_keys file;
  - delete the private/public key files and the launcher handoff file;
  - stop the OpenSSH server and set it to Manual start;
  - remove the Tailscale-only SSH firewall rule this script may have set.

Continue?
"@
            $agreed = $false
            try {
                Add-Type -AssemblyName System.Windows.Forms
                $r = [System.Windows.Forms.MessageBox]::Show($msg, 'Remove AI remote access',
                    [System.Windows.Forms.MessageBoxButtons]::YesNo,
                    [System.Windows.Forms.MessageBoxIcon]::Warning)
                $agreed = ($r -eq [System.Windows.Forms.DialogResult]::Yes)
            } catch {
                Write-Host $msg
                $agreed = ((Read-Host 'Type  yes  to continue').Trim().ToLower() -eq 'yes')
            }
            if (-not $agreed) { Write-Host 'Cancelled. Nothing was changed.'; return }
        }

        $pubLine = $null
        if (Test-Path -LiteralPath "$privPath.pub") {
            try { $pubLine = (Get-Content -Raw -LiteralPath "$privPath.pub").Trim() } catch {}
        }
        if (-not $pubLine -and (Test-Path -LiteralPath $handoffPath)) {
            try {
                $hj = Get-Content -Raw -LiteralPath $handoffPath | ConvertFrom-Json
                if ($hj.pub_line) { $pubLine = [string]$hj.pub_line }
            } catch {}
        }

        $authFile = Get-AiRemoteAuthFile -UserName $User -SshDir $sshDir
        if ($pubLine -and (Test-Path -LiteralPath $authFile)) {
            $kept = @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue |
                Where-Object { $_.Trim() -ne $pubLine })
            Set-Content -LiteralPath $authFile -Value $kept
            if ($authFile -like '*administrators_authorized_keys') {
                & icacls.exe $authFile /inheritance:r /grant 'SYSTEM:F' /grant 'BUILTIN\Administrators:F' | Out-Null
            }
            Write-Host "Removed the key line from $authFile."
        } else {
            Write-Host "No matching key line to remove (auth file: $authFile)."
        }

        foreach ($f in @($privPath, "$privPath.pub", $handoffPath)) {
            if (Test-Path -LiteralPath $f) {
                Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
                Write-Host "Deleted $f"
            }
        }

        $ruleName = 'OpenSSH-Server-In-TCP'
        if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
            # Only remove the rule when we had narrowed it to Tailscale; a stock
            # OpenSSH install may own the same name - leave a default-allow rule
            # alone by resetting rather than deleting if AllowLan was used.
            try { Remove-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue } catch {}
            Write-Host "Removed firewall rule $ruleName (if present)."
        }

        try {
            Stop-Service -Name sshd -ErrorAction SilentlyContinue
            Set-Service -Name sshd -StartupType Manual -ErrorAction SilentlyContinue
            Write-Host 'OpenSSH server stopped and set to Manual start.'
        } catch {
            Write-Host 'Could not stop sshd (it may not be installed).'
        }

        Write-Host ''
        Write-Host 'AI remote access has been removed from this computer.' -ForegroundColor Cyan
        return
    }

    # 1. Consent. A popup if a desktop is available, otherwise a console question.
    if (-not $IAgree) {
        $msg = @"
Let an AI on your OTHER computer control THIS computer?

It will be reachable only inside your own Tailscale network, and only to whoever
holds the key this script makes. The AI will be able to run commands, read and
write files, and do anything the user '$User' can do here.

This turns on the built-in Windows SSH server. You can undo it anytime (the
script prints how). Continue?
"@
        $agreed = $false
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $r = [System.Windows.Forms.MessageBox]::Show($msg, 'Set up AI remote access',
                [System.Windows.Forms.MessageBoxButtons]::YesNo,
                [System.Windows.Forms.MessageBoxIcon]::Warning)
            $agreed = ($r -eq [System.Windows.Forms.DialogResult]::Yes)
        } catch {
            Write-Host $msg
            $agreed = ((Read-Host 'Type  yes  to continue').Trim().ToLower() -eq 'yes')
        }
        if (-not $agreed) { Write-Host 'Cancelled. Nothing was changed.'; return }
    }

    # 2. OpenSSH server: install the optional feature, then start it for good.
    $cap = Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' } | Select-Object -First 1
    if (-not $cap) { throw 'OpenSSH.Server capability not found on this Windows edition.' }
    $restartNeeded = $false
    if ($cap.State -ne 'Installed') {
        Write-Host 'Installing the OpenSSH server (one-time, downloads from Windows Update)...'
        $addResult = Add-WindowsCapability -Online -Name $cap.Name
        if ($addResult -and $addResult.RestartNeeded) { $restartNeeded = $true }
    }

    # The install registers the 'sshd' service; on some builds it is not there
    # immediately, or the install needs a restart. Verify before using it, and
    # say so clearly instead of letting Set-Service throw "sshd was not found".
    if ($restartNeeded) {
        throw 'OpenSSH server was staged but needs a RESTART to finish. Reboot this computer, then run this script again.'
    }
    $sshdSvc = $null
    for ($i = 0; $i -lt 8 -and -not $sshdSvc; $i++) {
        $sshdSvc = Get-Service -Name sshd -ErrorAction SilentlyContinue
        if (-not $sshdSvc) { Start-Sleep -Seconds 1 }
    }
    if (-not $sshdSvc) {
        $capNow = (Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' } | Select-Object -First 1).State
        if ($capNow -eq 'Installed') {
            throw 'OpenSSH server is installed but its sshd service is missing. A RESTART usually registers it: reboot this computer, then run this script again.'
        }
        throw "OpenSSH server did not install (capability state: $capNow). Windows could not fetch it from Windows Update. Connect this computer to the internet and run again, or add it by hand in Settings > System > Optional features > Add a feature > OpenSSH Server, then run this script again."
    }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd
    # ssh-agent is handy but optional; do not fail if it is absent.
    try { Set-Service -Name ssh-agent -StartupType Automatic; Start-Service ssh-agent } catch {}
    Write-Host 'OpenSSH server is installed and running (starts automatically from now on).'

    # 3. The key. Either authorize a public key you pass in, or make a new pair here.
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    $pubLine = $null
    $handoverNote = $null
    if ($PublicKey) {
        $pubLine = $PublicKey.Trim()
        if ($pubLine -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-)') { throw 'That does not look like an SSH public key.' }
        $handoverNote = 'You passed a public key, so the private key stays on the other computer (nothing to hand over).'
    } else {
        if (-not (Test-Path -LiteralPath $privPath)) {
            Write-Host 'Making a new SSH key for this access...'
            # -N '""' is the empty passphrase; verify on your PowerShell version.
            & ssh-keygen -q -t ed25519 -f $privPath -N '""' -C "$KeyName@$env:COMPUTERNAME"
        }
        $pubLine = (Get-Content -Raw -LiteralPath "$privPath.pub").Trim()
        $handoverNote = "Carry this PRIVATE key file to the other computer: $privPath"
    }

    # 4. Authorize the public key (admin users need administrators_authorized_keys).
    $authFile = Get-AiRemoteAuthFile -UserName $User -SshDir $sshDir
    if (-not (Test-Path -LiteralPath $authFile)) { New-Item -ItemType File -Force -Path $authFile | Out-Null }
    $existing = @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue)
    if ($existing -notcontains $pubLine) { Add-Content -LiteralPath $authFile -Value $pubLine }
    if ($authFile -like '*administrators_authorized_keys') {
        # Required ACL: only SYSTEM and Administrators, nothing inherited.
        & icacls.exe $authFile /inheritance:r /grant 'SYSTEM:F' /grant 'BUILTIN\Administrators:F' | Out-Null
        Write-Host "Authorized the key in $authFile (admin user)."
    } else {
        Write-Host "Authorized the key in $authFile (user $User)."
    }

    # 5. Firewall: by default allow the SSH port ONLY from Tailscale addresses.
    $ruleName = 'OpenSSH-Server-In-TCP'
    if (-not $AllowLan) {
        if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
            Set-NetFirewallRule -Name $ruleName -RemoteAddress '100.64.0.0/10'
        } else {
            New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH SSH Server (sshd)' -Enabled True `
                -Direction Inbound -Protocol TCP -Action Allow -LocalPort $Port -RemoteAddress '100.64.0.0/10' | Out-Null
        }
        Write-Host "Firewall: SSH ($Port) allowed from Tailscale (100.64.0.0/10) only."
    } else {
        Write-Host "Firewall: left SSH ($Port) open to all networks (-AllowLan)."
    }

    # 6. Find this computer's Tailscale address for the connection line.
    $tsExe = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
    if (-not $tsExe) { foreach ($p in "$env:ProgramFiles\Tailscale\tailscale.exe", "$env:ProgramFiles(x86)\Tailscale\tailscale.exe") { if (Test-Path $p) { $tsExe = $p; break } } }
    $tsip = $null
    if ($tsExe) {
        try { $tsip = (& $tsExe ip -4 2>$null | Select-Object -First 1).Trim() } catch {}
    }
    $host4 = if ($tsip) { $tsip } else { '<this computer''s Tailscale IP>' }

    # 7. Print what to take to the other computer, and persist it for the launcher.
    $keyForLine = if ($PublicKey) { '<the private key file>' } else { $privPath }
    $promptLine = ("Control my computer {0} over SSH:  ssh -i {1} -p {2} {3}@{4}" -f $env:COMPUTERNAME, $keyForLine, $Port, $User, $host4)
    Write-Host ''
    Write-Host '==================================================================' -ForegroundColor Cyan
    Write-Host ' Done. On your OTHER computer, give Claude Code / Codex this:' -ForegroundColor Cyan
    Write-Host '==================================================================' -ForegroundColor Cyan
    Write-Host $promptLine
    Write-Host $handoverNote
    if (-not $tsip) { Write-Host 'Tailscale address not found yet: install/log in to Tailscale, then run  tailscale ip -4  for the address.' }
    Write-Host ''
    Write-Host 'To revoke later: use the launcher ''Remove AI remote access'' button,'
    Write-Host 'or delete that key line from the authorized_keys file above,'
    Write-Host 'or  Stop-Service sshd; Set-Service sshd -StartupType Manual  to turn SSH off.'

    # Launcher reads this file so closing this window does not lose the two lines.
    $handoff = [ordered]@{
        computer      = $env:COMPUTERNAME
        user          = $User
        port          = $Port
        host          = $host4
        key_path      = $(if ($PublicKey) { '' } else { $privPath })
        auth_file     = $authFile
        pub_line      = $pubLine
        prompt_line   = $promptLine
        handover_note = $handoverNote
        updated_at    = (Get-Date).ToString('o')
    }
    ($handoff | ConvertTo-Json) | Set-Content -LiteralPath $handoffPath -Encoding UTF8
    Write-Host "Also saved for the MAST launcher: $handoffPath"
}
catch {
    Write-Host ''
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Setup did not finish. Fix the problem above and run this again.'
}
finally {
    # Keep the window open so the connection line / key path / error stays visible.
    # Skipped for -IAgree (unattended installs must not block on input).
    if (-not $IAgree) {
        Write-Host ''
        try { [void](Read-Host 'Press Enter to close this window') } catch {}
    }
}
