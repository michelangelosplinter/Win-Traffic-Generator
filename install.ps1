<#
.SYNOPSIS
    One-command setup for win-rdp-mcp.

.DESCRIPTION
    Builds the virtualenv, installs dependencies, seeds the config files, checks
    that Ollama is present with a vision model, and verifies the whole chain end
    to end.

    Run from the repo root:
        powershell -ExecutionPolicy Bypass -File .\install.ps1

.PARAMETER PullModel
    Also download the vision model if it is missing (about 6 GB).

.PARAMETER Model
    Which Ollama model to expect. Defaults to qwen2.5vl:7b.

.PARAMETER Python
    Path to a Python 3.10+ interpreter. Auto-detected if omitted.
#>
[CmdletBinding()]
param(
    [switch]$PullModel,
    [string]$Model = "qwen2.5vl:7b",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg)  { Write-Host "  $msg" }
function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

Write-Host "win-rdp-mcp setup" -ForegroundColor Green
Write-Host "  repo: $root"

# ---------------------------------------------------------------- platform
Step "Checking platform"
if ($env:OS -ne "Windows_NT") {
    throw "This server drives the Windows Remote Desktop client (mstsc.exe) and only runs on Windows."
}
$mstsc = Join-Path $env:SystemRoot "System32\mstsc.exe"
if (-not (Test-Path $mstsc)) { throw "mstsc.exe not found at $mstsc - Remote Desktop Connection is required." }
Say "Windows OK, mstsc.exe present"

# ---------------------------------------------------------------- python
Step "Locating Python"
if ($Python -eq "") {
    foreach ($candidate in @("py -3.11", "py -3", "python")) {
        $parts = $candidate.Split(" ")
        $cmd = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                if ($parts.Count -gt 1) { $v = & $parts[0] $parts[1] --version 2>&1 }
                else                    { $v = & $parts[0] --version 2>&1 }
                if ($v -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
                    $Python = $candidate
                    Say "found $v via '$candidate'"
                    break
                }
            } catch { }
        }
    }
}
if ($Python -eq "") { throw "No Python 3.10+ found. Install from https://python.org and re-run, or pass -Python <path>." }

# ---------------------------------------------------------------- venv
Step "Creating virtualenv (.venv)"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Say ".venv already exists, reusing it"
} else {
    $p = $Python.Split(" ")
    if ($p.Count -gt 1) { & $p[0] $p[1] -m venv .venv } else { & $p[0] -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    Say "created .venv"
}

Step "Installing dependencies"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
Say "dependencies installed"

# pywin32 needs a post-install step for its DLLs on some systems.
$post = Join-Path $root ".venv\Scripts\pywin32_postinstall.py"
if (Test-Path $post) {
    & $venvPy $post -install 2>&1 | Out-Null
    Say "pywin32 post-install done"
}

# ---------------------------------------------------------------- config
Step "Seeding configuration"
$job = Join-Path $root "job.json"
if (Test-Path $job) {
    Say "job.json already exists, leaving it alone"
} else {
    Copy-Item (Join-Path $root "job.example.json") $job
    Warn "created job.json from the example - EDIT IT (hosts, logins, actions)"
}

# ---------------------------------------------------------------- ollama
Step "Checking Ollama"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $guess) { $ollama = @{ Source = $guess } }
}
if (-not $ollama) {
    Warn "Ollama is not installed. The RDP tools will work, but nothing can drive them."
    Say  "Install it, then re-run this script:"
    Say  "    winget install --id Ollama.Ollama -e"
} else {
    $exe = $ollama.Source
    Say "found $exe"

    $up = $false
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/version" -UseBasicParsing -TimeoutSec 5
        $up = $true
    } catch { }
    if ($up) { Say "server responding on 127.0.0.1:11434" }
    else     { Warn "Ollama is installed but not responding - launch it from the Start menu" }

    $have = & $exe list 2>&1 | Out-String
    if ($have -match [regex]::Escape($Model)) {
        Say "model $Model is present"
    } elseif ($PullModel) {
        Say "pulling $Model (about 6 GB, this takes a while) ..."
        & $exe pull $Model
        if ($LASTEXITCODE -eq 0) { Say "pulled $Model" } else { Warn "pull failed - run '$exe pull $Model' by hand" }
    } else {
        Warn "model $Model is not downloaded. Get it with:"
        Say  "    ollama pull $Model"
        Say  "or re-run this script with -PullModel"
    }
}

# ---------------------------------------------------------------- verify
Step "Verifying"
$check = & $venvPy -c "import win32gui, mss, PIL, mcp; print('imports OK')" 2>&1
if ($LASTEXITCODE -ne 0) { throw "import check failed: $check" }
Say $check

Say "checking the MCP chain ..."
$self = & $venvPy (Join-Path $root "ollama_agent.py") --selftest 2>&1 | Out-String
if ($self -match "MCP wiring OK") { Say "MCP wiring OK" } else { Warn "selftest did not report OK:`n$self" }

# ---------------------------------------------------------------- done
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit job.json - add your hosts, their logins, and the actions to run."
Write-Host "  2. Run it:"
Write-Host "       .\.venv\Scripts\python.exe ollama_agent.py --job job.json"
Write-Host ""
Warn "  Leave the machine alone while it runs: the Remote Desktop window has to"
Warn "  hold the local foreground, so the mouse pointer is in use."
