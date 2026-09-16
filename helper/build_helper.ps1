<#
.SYNOPSIS
    Freeze poke.py into a single self-contained poke.exe for the target VMs.

.DESCRIPTION
    The VMs have NO Python, so the Phase 0 tool ships as one .exe. The one real
    trap is comtypes: uiautomation calls comtypes.client.GetModule("UIAutomation
    Core.dll") at first use, which GENERATES a wrapper module. If that wrapper is
    not present in the frozen app, the exe dies on first UIA call. So this script
    PRE-GENERATES the wrapper in the build venv, then tells PyInstaller to
    --collect-all comtypes (which now includes that fresh wrapper).

    Run from the helper\ directory:
        powershell -ExecutionPolicy Bypass -File .\build_helper.ps1

    Output: dist\poke.exe

.PARAMETER Python
    Path to the build venv's python.exe. Defaults to .venv-helper\Scripts\python.exe
    beside this script. MUST be a stable 3.10-3.13 (see requirements-helper.txt).

.PARAMETER Script
    Which entry point to freeze. Defaults to poke.py; pass helper.py later.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Script = "poke.py"
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ($Python -eq "") { $Python = Join-Path $root ".venv-helper\Scripts\python.exe" }
if (-not (Test-Path $Python)) {
    Write-Host "x build venv python not found at: $Python" -ForegroundColor Red
    Write-Host "  Create it first:" -ForegroundColor Yellow
    Write-Host "    py -3.11 -m venv .venv-helper"
    Write-Host "    .\.venv-helper\Scripts\python.exe -m pip install -r requirements-helper.txt"
    exit 1
}

# Run a native command WITHOUT PowerShell 5.1 wrapping its stderr in
# ErrorRecords (which, with -ErrorActionPreference Stop, hides the real error).
function Invoke-Native {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    $errFile = [System.IO.Path]::GetTempFileName()
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>$errFile | Out-String
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    $err = ""
    if (Test-Path $errFile) { $err = Get-Content $errFile -Raw; Remove-Item $errFile -Force -ErrorAction SilentlyContinue }
    [pscustomobject]@{ Out = $out; Err = $err; Code = $code }
}

Write-Host "win-rdp helper build" -ForegroundColor Green
$ver = (Invoke-Native $Python @("--version")).Out.Trim()
Write-Host "  python: $ver  ($Python)"
if ($ver -match "\d+(a|b|rc)\d+") {
    Write-Host "  x that is a PRE-RELEASE Python - uiautomation/comtypes/pillow have no wheels for it." -ForegroundColor Red
    Write-Host "    Build with a stable 3.10-3.13 instead." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "==> Pre-generating the comtypes UIAutomationCore wrapper" -ForegroundColor Cyan
$gen = Invoke-Native $Python @("-c", "import uiautomation as a; a.SetGlobalSearchTimeout(2); a.GetRootControl(); import comtypes.gen as g; print('gen at', g.__path__[0])")
if ($gen.Code -ne 0) {
    Write-Host "  x pre-generation failed - the exe would fail the same way at runtime:" -ForegroundColor Red
    foreach ($l in ($gen.Err -split "`r?`n")) { if ($l.Trim()) { Write-Host "      $l" -ForegroundColor DarkGray } }
    exit 1
}
Write-Host "  $($gen.Out.Trim())"

Write-Host ""
Write-Host "==> Freezing $Script with PyInstaller (onefile)" -ForegroundColor Cyan
$name = [System.IO.Path]::GetFileNameWithoutExtension($Script)
$piArgs = @(
    "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm",
    "--name", $name,
    "--paths", ".",
    "--collect-all", "comtypes",
    "--collect-all", "uiautomation",
    "--hidden-import", "PIL.ImageGrab",
    $Script
)
$build = Invoke-Native $Python $piArgs
foreach ($l in ($build.Out -split "`r?`n")) { if ($l -match "ERROR|WARNING: Hidden|completed successfully|Building EXE") { Write-Host "      $l" -ForegroundColor DarkGray } }
if ($build.Code -ne 0) {
    Write-Host "  x PyInstaller failed:" -ForegroundColor Red
    foreach ($l in ($build.Err -split "`r?`n")) { if ($l.Trim()) { Write-Host "      $l" -ForegroundColor DarkGray } }
    exit 1
}

$exe = Join-Path $root ("dist\{0}.exe" -f $name)
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Built: $exe  (${mb} MB)" -ForegroundColor Green
    Write-Host ""
    Write-Host "Smoke-test it locally BEFORE copying to a VM:"
    if ($name -eq "poke") {
        Write-Host "    .\dist\poke.exe windows"
        Write-Host "    .\dist\poke.exe launch notepad"
        Write-Host "    .\dist\poke.exe observe --title Notepad"
    } elseif ($name -eq "helper") {
        Write-Host "    .\dist\helper.exe --version"
        Write-Host "    # dial your controller (Phase 2):"
        Write-Host "    .\dist\helper.exe --connect CONTROLLER:8765 --token <TOKEN> --host-label vm-1"
    } else {
        Write-Host "    .\dist\$name.exe --help"
    }
} else {
    Write-Host "x expected $exe but it is not there" -ForegroundColor Red
    exit 1
}
