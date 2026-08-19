# Turn this Windows machine into a Forge worker.
#
#   .\run-node.ps1 -Server http://your-nas:58420 -Mounts '{"server":"/media","local":"Z:/Media"}'
#
# Runs natively so NVENC and QuickSync work directly. Docker Desktop can do
# GPU work through WSL2, but it needs the NVIDIA Container Toolkit and adds a
# layer for no benefit when the machine is sitting right here.
#
# If PowerShell refuses to run this, it's the execution policy:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

param(
    [Parameter(Mandatory = $true)]
    [string]$Server,

    # Where this machine sees the share. Usually just the drive:
    #   -Mounts Z:\Media
    # A UNC path works too:  -Mounts \\nas\media
    # If the server calls it something other than /media, say so:
    #   -Mounts Z:\Media -ServerPath /library
    # Full JSON is still accepted for anything unusual.
    # Leave it out and files are copied over the network instead.
    [string]$Mounts = "",

    # What the Forge server calls the same share. Matches MEDIA_ROOTS and
    # the paths you typed into your libraries.
    [string]$ServerPath = "/media",

    [string]$NodeName = $env:COMPUTERNAME,
    [int]$MaxJobs = 1,
    [string]$WorkDir = "$env:TEMP\forge"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path "worker\agent.py")) {
    Write-Host ""
    Write-Host "This needs to run from inside the Forge folder." -ForegroundColor Red
    Write-Host ""
    Write-Host "It's looking in:  $PSScriptRoot"
    Write-Host "and can't find:   worker\agent.py"
    Write-Host ""
    Write-Host "Unzip the release somewhere, then run it from that folder."
    exit 1
}

function Fail($message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- checks

# Windows ships a placeholder python.exe that only opens the Microsoft
# Store, so merely finding the command proves nothing. Each candidate is
# asked its version and has to actually answer.
function Find-Python {
    foreach ($candidate in @(
            @{ Exe = "py";      Args = @("-3") },
            @{ Exe = "python3"; Args = @() },
            @{ Exe = "python";  Args = @() })) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $reported = & $candidate.Exe @($candidate.Args + "--version") 2>&1 | Out-String
        }
        catch { continue }
        if ($LASTEXITCODE -eq 0 -and $reported -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 9) { return $candidate }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Fail @"
Python isn't installed.

Windows has a placeholder that pretends it is, which is why you saw
"Python was not found; run without arguments to install from the
Microsoft Store". Install the real thing:

  winget install Python.Python.3.12

Then close this window and open a new one, so the PATH change applies.

If it still isn't found afterwards, turn off the placeholder:
  Settings > Apps > Advanced app settings > App execution aliases
  and switch off both entries named python.exe and python3.exe
"@
}

$pythonExe = $python.Exe
$pythonArgs = $python.Args

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Fail @"
FFmpeg isn't installed, or isn't on your PATH.

  winget install Gyan.FFmpeg

Then open a new terminal so the PATH change takes effect. A full build is
needed — the one bundled with some programs leaves out NVENC.
"@
}

# A bare address is almost always meant as http, and a plain HTTP server on
# the network is the normal case for a worker.
if ($Server -notmatch "^https?://") { $Server = "http://$Server" }
$Server = $Server.TrimEnd("/")

# TLS on a private address is nearly always a mistake: the certificate and
# the proxy live on the public name, not on 192.168.x.x.
if ($Server -match "^https://(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|localhost|127\.)") {
    $plain = $Server -replace "^https://", "http://"
    Write-Host ""
    Write-Host "That address is on your own network but written as https." -ForegroundColor Yellow
    Write-Host "Forge itself speaks plain http; only the reverse proxy adds TLS."
    Write-Host "Trying $plain instead."
    Write-Host ""
    $Server = $plain
}

# A worker pointed at a proxy works for control traffic but not for file
# transfers: proxies cap request bodies and time out long uploads.
if ($Server -match "^https://" -or $Server -match "synology\.me|duckdns\.org|ddns\.net") {
    Write-Host ""
    Write-Host "Note: that looks like a public or proxied address." -ForegroundColor Yellow
    Write-Host "Workers are better pointed straight at the NAS, for example:"
    Write-Host "  .\run-node.ps1 -Server http://192.168.1.50:58420"
    if ($Mounts -eq "[]") {
        Write-Host ""
        Write-Host "You also haven't set -Mounts, so every file would be copied"
        Write-Host "across the network rather than read from the share directly."
    }
    Write-Host ""
    $answer = Read-Host "Carry on anyway? [y/N]"
    if ($answer -notmatch "^[yY]") { exit 1 }
}

try {
    Invoke-WebRequest -Uri "$Server/api/state" -TimeoutSec 5 -UseBasicParsing | Out-Null
}
catch {
    # Before giving up, see whether the other scheme answers — that is the
    # usual cause and the message should say so rather than list guesses.
    $other = if ($Server -match "^https://") { $Server -replace "^https://", "http://" }
             else { $Server -replace "^http://", "https://" }
    try {
        Invoke-WebRequest -Uri "$other/api/state" -TimeoutSec 5 -UseBasicParsing | Out-Null
        Write-Host ""
        Write-Host "$Server didn't answer, but $other does." -ForegroundColor Yellow
        Write-Host "Using that."
        Write-Host ""
        $Server = $other
    }
    catch {
        Fail @"
Can't reach Forge at $Server

Neither http nor https answered on that address.

Things to check:
  - Is the Forge container running on the NAS?
  - Is the port published? The stack maps 58420 to the container's 8420.
  - Ping works but the port doesn't, which usually means a firewall on the
    NAS, or the container is bound to a different address.

From here, try:
  curl http://192.168.1.163:58420/api/state
"@
    }
}

# Accept the thing a person would actually type. A bare path is by far the
# most common, so that comes first; JSON still works for odd setups.
function Resolve-Mounts([string]$value, [string]$serverPath) {
    $value = $value.Trim()
    if (-not $value -or $value -eq "[]") { return "[]" }
    if ($value.StartsWith("[")) { return $value }
    if ($value.StartsWith("{")) { return "[$value]" }

    # "/media=Z:\Media" for when both sides need saying.
    if ($value -match "^(?<server>[^=]+)=(?<local>.+)$") {
        $serverPath = $Matches.server.Trim()
        $value = $Matches.local.Trim()
    }

    # Forward slashes throughout, so the server can match the two paths up.
    $local = $value.TrimEnd([char]92, [char]47).Replace([char]92, [char]47)
    $server = "/" + $serverPath.Trim().Trim("/")
    $one = @{ server = $server; local = $local } | ConvertTo-Json -Compress
    return "[$one]"
}

$Mounts = Resolve-Mounts $Mounts $ServerPath

# A path that isn't there means every job would fail on this machine.
if ($Mounts -ne "[]") {
    $localPath = ([regex]::Match($Mounts, '"local"\s*:\s*"([^"]+)"')).Groups[1].Value
    if ($localPath -and -not (Test-Path $localPath)) {
        Write-Host ""
        Write-Host "Warning: $localPath doesn't exist on this machine." -ForegroundColor Yellow
        Write-Host "Check the drive is mapped and connected, or Forge will not"
        Write-Host "be able to open any of the files it is sent."
        Write-Host ""
    }
}

# --------------------------------------------------------------- set up

$venvPython = ".\.venv-node\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Setting up (once)..."
    & $pythonExe @($pythonArgs + @("-m", "venv", ".venv-node"))
    if (-not (Test-Path $venvPython)) {
        Fail @"
Couldn't create the Python environment in .venv-node

Try running this by hand to see what it says:
  $pythonExe -m venv .venv-node
"@
    }
}
& $venvPython -m pip install -q --disable-pip-version-check -r worker\requirements.txt
if ($LASTEXITCODE -ne 0) {
    Fail "Couldn't install what the worker needs. Check the messages above."
}

Write-Host ""
Write-Host "Connecting to $Server as `"$NodeName`"" -ForegroundColor Green
if ($Mounts -ne "[]") {
    Write-Host "Share mapping: $Mounts"
}
else {
    Write-Host "No share mapping given - files will be copied over the network."
    Write-Host "Pass -Mounts to read them directly, which is much faster."
}
Write-Host "It will measure this machine's encoders once, which takes a minute."
Write-Host "Press Ctrl-C to stop."
Write-Host ""

$env:SERVER = $Server
$env:NODE_NAME = $NodeName
$env:MOUNTS = $Mounts
$env:MAX_JOBS = "$MaxJobs"
$env:WORK_DIR = $WorkDir
$env:PYTHONUNBUFFERED = "1"

& $venvPython worker\agent.py
