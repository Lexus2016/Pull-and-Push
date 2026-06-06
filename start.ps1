# Pull-and-Push - one-command install & run (Windows / PowerShell).
#
# Creates a CPython virtualenv (.venv), installs the package, and launches the
# dashboard. Safe to re-run: steps already done are skipped. Extra arguments are
# passed straight to `pull-and-push web`.
#
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 --port 8080
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 --update   # reinstall deps
#
$ErrorActionPreference = 'Stop'
# native commands (the python/pip probes) signal via exit code, not exceptions — don't let a
# non-zero code or a pip stderr notice abort the script (PowerShell 7.4+ defaults this to $true).
$PSNativeCommandUseErrorActionPreference = $false
Set-Location $PSScriptRoot

$venv = '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'

# split off our own --update flag; everything else goes to the dashboard
$update = $false
$pass = @()
foreach ($a in $args) {
    if ($a -eq '--update' -or $a -eq '-Update') { $update = $true } else { $pass += $a }
}

# Find a usable CPython 3.10+ (NOT PyPy) to build the venv with.
function Find-Python {
    $probe = "import sys,platform; sys.exit(0 if platform.python_implementation()=='CPython' and sys.version_info[:2]>=(3,10) else 1)"
    $cands = @(
        @('py', '-3.12'),
        @('py', '-3.11'),
        @('py', '-3.13'),
        @('py', '-3.10'),
        @('py', '-3'),
        @('python'),
        @('python3')
    )
    foreach ($c in $cands) {
        $exe = $c[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
        & $exe @pre -c $probe 2>$null
        if ($LASTEXITCODE -eq 0) { return ,$c }
    }
    return $null
}

# 1. virtualenv
if (-not (Test-Path $venv)) {
    $found = Find-Python
    if (-not $found) {
        Write-Host "Need CPython 3.10+ (not PyPy). Install it from https://www.python.org/downloads/" -ForegroundColor Red
        Write-Host "(tick 'Add python.exe to PATH'), then run start.ps1 again." -ForegroundColor Red
        exit 1
    }
    $exe = $found[0]; $pre = @(); if ($found.Count -gt 1) { $pre = $found[1..($found.Count - 1)] }
    Write-Host "> Creating virtualenv ..."
    & $exe @pre -m venv $venv
    $update = $true   # fresh venv -> must install
}

# 2. install (first run, or --update, or if the package isn't importable)
$importable = $false
if (Test-Path $py) { & $py -c "import tyani_tolkai" 2>$null; $importable = ($LASTEXITCODE -eq 0) }
if ($update -or -not $importable) {
    Write-Host "> Installing dependencies (.[web]) ..."
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -e ".[web]"
}

# 3. launch the dashboard - prefer the console script, fall back to `python -m`
Write-Host "> Starting the dashboard - open the URL printed below (Ctrl-C to stop)."
$exe2 = Join-Path $venv 'Scripts\pull-and-push.exe'
if (Test-Path $exe2) { & $exe2 web @pass }
else { & $py -m tyani_tolkai.cli web @pass }
