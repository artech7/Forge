# Keeps the Forge worker running indefinitely: through crashes, through the
# computer sleeping and waking, and - once installed via
# install-startup-task.ps1 - through sign-outs, Remote Desktop disconnects,
# and reboots.
#
# Run directly, it restarts the worker if it ever exits, forever, until you
# close this window or create a file named ".stop" next to this script:
#
#   .\worker\run-node-forever.ps1 -Server http://192.168.1.163:58420 -Mounts Z:\Media
#
# Every parameter run-node.ps1 accepts works here too - they're passed
# straight through to it. This script itself is the thing that should be
# installed to survive reboots (see install-startup-task.ps1), not
# run-node.ps1 directly, since run-node.ps1 only runs once and stops.
#
# Output isn't shown on screen when running unattended - it all goes to
# the log file below. Follow it live with:
#   Get-Content -Wait -Tail 50 "$env:TEMP\forge\logs\worker.log"

param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$Mounts = "",
    [string]$ServerPath = "/media",
    [string]$NodeName = $env:COMPUTERNAME,
    [int]$MaxJobs = 1,
    [string]$WorkDir = "$env:TEMP\forge",
    [string]$LogDir = ""
)

# A single bad run must never take the supervisor down with it - that
# would defeat the entire point of this script.
$ErrorActionPreference = "Continue"
Set-Location "$PSScriptRoot\.."

if (-not $LogDir) { $LogDir = Join-Path $WorkDir "logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logFile = Join-Path $LogDir "worker.log"
$stopFile = Join-Path $PSScriptRoot ".stop"

function Write-Log($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

# A log left running for a year unwatched is a problem of its own. One
# previous rotation is kept rather than none, so a crash right after a
# rotation doesn't lose the only copy of what just happened.
function Rotate-LogIfLarge {
    if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 20MB)) {
        $old = Join-Path $LogDir "worker.log.old"
        Remove-Item $old -ErrorAction SilentlyContinue
        Rename-Item $logFile $old
    }
}

Write-Log "======================================================"
Write-Log "Supervisor starting (PID $PID). Delete this file to stop cleanly:"
Write-Log "  $stopFile"

$runnerArgs = @{
    Server     = $Server
    ServerPath = $ServerPath
    NodeName   = $NodeName
    MaxJobs    = $MaxJobs
    WorkDir    = $WorkDir
}
if ($Mounts) { $runnerArgs.Mounts = $Mounts }

# NOTE: run-node.ps1 has one interactive prompt, asking for confirmation
# when -Server looks like a public or proxied address (https, or a
# synology.me/duckdns.org/ddns.net name). With nothing attached to answer
# it, that prompt would hang forever under a Scheduled Task. It's not
# reachable with a plain local address like http://192.168.1.x - if the
# server address ever changes to a public/proxied one, that check is
# worth revisiting.

$backoff = 5   # seconds; grows on repeated fast failures, resets after a healthy run
while ($true) {
    if (Test-Path $stopFile) {
        Write-Log "Stop file found. Exiting - the worker will not restart until this script runs again."
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        break
    }

    Rotate-LogIfLarge
    Write-Log "Starting worker (Server=$Server, NodeName=$NodeName, MaxJobs=$MaxJobs)..."
    $started = Get-Date
    try {
        & "$PSScriptRoot\..\run-node.ps1" @runnerArgs *>> $logFile
        $exitCode = $LASTEXITCODE
    }
    catch {
        Write-Log "Supervisor caught an error launching the worker: $_"
        $exitCode = -1
    }
    $ran = (Get-Date) - $started
    Write-Log "Worker stopped (exit code $exitCode) after $([int]$ran.TotalSeconds)s."

    # A run that lasted a while wasn't a crash loop - treat it as healthy
    # and reset the wait, rather than the backoff creeping up over months
    # of otherwise-normal restarts (e.g. after every reboot).
    if ($ran.TotalSeconds -gt 120) { $backoff = 5 }
    else { $backoff = [Math]::Min($backoff * 2, 300) }

    Write-Log "Restarting in $backoff seconds..."
    Start-Sleep -Seconds $backoff
}
