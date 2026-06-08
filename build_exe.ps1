# build_exe.ps1 — Package ScanToBIM into a standalone Windows executable (no license system).
# Uses ScanToBIM.spec for full control over bundled packages.
#
# Output:  dist\ScanToBIM\   ← zip this entire folder to distribute
#
# Run from the ScanToBIM-BuiltCoLAB folder:
#   .\build_exe.ps1

Set-Location $PSScriptRoot
$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"

Write-Host "=== ScanToBIM - Build Standalone EXE (No License) ===" -ForegroundColor Cyan
Write-Host ""

# 1. Ensure PyInstaller is installed
Write-Host "[1/3] Checking PyInstaller..." -ForegroundColor Yellow
& $python -m pip install pyinstaller --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install pyinstaller failed"; exit 1 }
Write-Host "      OK" -ForegroundColor Green

# 2. Clean previous build artefacts
Write-Host "[2/3] Cleaning old build..." -ForegroundColor Yellow
if (Test-Path "dist\ScanToBIM")  { Remove-Item -Recurse -Force "dist\ScanToBIM" }
if (Test-Path "build\ScanToBIM") { Remove-Item -Recurse -Force "build\ScanToBIM" }
Write-Host "      OK" -ForegroundColor Green

# 3. Run PyInstaller with the spec file
Write-Host "[3/3] Building (first run takes 5-15 min)..." -ForegroundColor Yellow
& $python -m PyInstaller ScanToBIM.spec --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed"; exit 1 }

Write-Host ""
Write-Host "=== BUILD COMPLETE ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Executable : dist\ScanToBIM\ScanToBIM.exe"
Write-Host "  To share   : zip the entire  dist\ScanToBIM\  folder"
Write-Host ""
Write-Host "  User instructions:"
Write-Host "    1. Unzip ScanToBIM.zip anywhere"
Write-Host "    2. Double-click ScanToBIM.exe"
Write-Host "    3. Browser opens automatically at http://127.0.0.1:8000"
Write-Host "    4. Import your E57 scan and IFC model to begin"
Write-Host ""
