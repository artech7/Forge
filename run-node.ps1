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

    # How this machine's paths line up with the server's. The server sees the
    # share at one path, Windows sees it at another.
    #   -Mounts '{"server":"/media","local":"Z:/Media"}'
    # A UNC path works too: "local":"//nas/media"
    # Leave it out and files are copied over the network instead.
    [string]$Mounts = "[]",

    [string]$NodeName = $env:COMPUTERNAME,
    [int]$MaxJobs = 1,
    [string]$WorkDir = "$env:TEMP\forge"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- checks

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail @"
Python isn't installed, or isn't on your PATH.

Get it from python.org and tick "Add python.exe to PATH" during setup,
or run:  winget install Python.Python.3.12
"@
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Fail @"
FFmpeg isn't installed, or isn't on your PATH.

  winget install Gyan.FFmpeg

Then open a new terminal so the PATH change takes effect. A full build is
needed — the one bundled with some programs leaves out NVENC.
"@
}

try {
    Invoke-WebRequest -Uri "$Server/api/state" -TimeoutSec 5 -UseBasicParsing | Out-Null
}
catch {
    Fail @"
Can't reach Forge at $Server

Check the server is running, and that this machine can see the NAS. Use the
NAS's address, not localhost. From here, try:
  curl $Server/api/state
"@
}

# A single JSON object is easier to type than an array, so accept either.
if ($Mounts.TrimStart().StartsWith("{")) { $Mounts = "[$Mounts]" }

# --------------------------------------------------------------- set up

if (-not (Test-Path ".venv-node")) {
    Write-Host "Setting up (once)..."
    python -m venv .venv-node
}
& ".\.venv-node\Scripts\python.exe" -m pip install -q -r worker\requirements.txt

Write-Host ""
Write-Host "Connecting to $Server as `"$NodeName`"" -ForegroundColor Green
if ($Mounts -ne "[]") {
    Write-Host "Reading files directly from the share."
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

& ".\.venv-node\Scripts\python.exe" worker\agent.py
