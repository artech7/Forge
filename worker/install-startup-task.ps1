# Makes the Forge worker run in the background permanently on this machine:
#   - starts automatically when the computer boots, before anyone signs in
#   - keeps running whether you're signed in, signed out, or disconnected
#     from Remote Desktop - it isn't tied to your session at all
#   - restarts itself if it ever crashes (run-node-forever.ps1 handles this;
#     the task itself also restarts as a second layer of protection)
#   - only stops if you stop it yourself, or the computer is off
#
# Run this ONCE, from an elevated PowerShell ("Run as Administrator"):
#
#   .\worker\install-startup-task.ps1 -Server http://192.168.1.163:58420 -Mounts Z:\Media
#
# To check on it later:
#   Get-ScheduledTask -TaskName "Forge Worker" | Get-ScheduledTaskInfo
#   Get-Content -Wait -Tail 50 "$env:TEMP\forge\logs\worker.log"
#
# To stop it permanently:
#   Unregister-ScheduledTask -TaskName "Forge Worker" -Confirm:$false
#
# To stop it just for now (it'll pick back up on the next reboot):
#   Disable-ScheduledTask -TaskName "Forge Worker"

param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$Mounts = "",
    [string]$ServerPath = "/media",
    [string]$NodeName = $env:COMPUTERNAME,
    [int]$MaxJobs = 1,
    [string]$WorkDir = "$env:TEMP\forge",
    [string]$TaskName = "Forge Worker"
)

$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host "This needs an elevated PowerShell." -ForegroundColor Red
    Write-Host "Right-click PowerShell (or Windows Terminal), choose"
    Write-Host "'Run as Administrator', then run this script again."
    exit 1
}

$scriptPath = Join-Path $PSScriptRoot "run-node-forever.ps1"
if (-not (Test-Path $scriptPath)) {
    Write-Host "Can't find $scriptPath" -ForegroundColor Red
    exit 1
}

# A mapped drive letter belongs to a sign-in session, not to the computer -
# it's the single most common reason a background task can't see its
# files. A UNC path has no such dependency.
if ($Mounts -match "^[A-Za-z]:\\") {
    Write-Host ""
    Write-Host "Heads up: $Mounts is a mapped drive letter." -ForegroundColor Yellow
    Write-Host "Drive letters belong to a sign-in session. Even running as your"
    Write-Host "own account, if this task runs while you're fully signed out"
    Write-Host "rather than just disconnected, Windows may not reconnect it -"
    Write-Host "and every job would fail with 'path not found'."
    Write-Host ""
    Write-Host "A UNC path avoids this entirely and is more reliable for"
    Write-Host "anything meant to run unattended, e.g.:"
    Write-Host "  .\worker\install-startup-task.ps1 -Server $Server -Mounts \\NAS\Media"
    Write-Host ""
    $answer = Read-Host "Continue with $Mounts anyway? [y/N]"
    if ($answer -notmatch "^[yY]") { exit 1 }
}

$argParts = @("-Server", $Server, "-ServerPath", $ServerPath,
             "-NodeName", $NodeName, "-MaxJobs", $MaxJobs, "-WorkDir", $WorkDir)
if ($Mounts) { $argParts += @("-Mounts", $Mounts) }
$quotedArgs = ($argParts | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
}) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" $quotedArgs" `
    -WorkingDirectory (Split-Path $PSScriptRoot -Parent)

$trigger = New-ScheduledTaskTrigger -AtStartup

# ExecutionTimeLimit of zero means "no limit" to Task Scheduler - without
# this, Windows kills the task after its default 3-day cap, which is
# exactly what "runs all year" can't tolerate. RestartCount/RestartInterval
# add a second layer of recovery on top of the supervisor script's own
# restart loop, in case powershell.exe itself ever dies outright.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Write-Host ""
Write-Host "Running whether signed in or not needs your Windows password,"
Write-Host "stored the same encrypted way any such task stores it - this"
Write-Host "is standard for anything Windows runs unattended."
Write-Host ""
$cred = Get-Credential -UserName $env:USERNAME `
    -Message "Confirm your Windows password so Forge can run in the background"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
    -RunLevel Limited `
    -Description ("Runs the Forge transcoding worker in the background, " +
                  "starting at boot and restarting itself if it ever stops.") `
    | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Installed and started." -ForegroundColor Green
Write-Host "This machine will now run Forge continuously - through reboots,"
Write-Host "sign-outs, and Remote Desktop disconnects - until you remove it:"
Write-Host "  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
Write-Host ""
Write-Host "Check on it:"
Write-Host "  Get-ScheduledTask -TaskName `"$TaskName`" | Get-ScheduledTaskInfo"
Write-Host "  Get-Content -Wait -Tail 50 `"$WorkDir\logs\worker.log`""
