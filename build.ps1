# Compileert de Product Image Classifier tot een deelbare Windows-map.
#
# Doet alles wat nodig is voor een nieuwe compilatie:
#   1. installeert/actualiseert de runtime- en build-afhankelijkheden
#   2. bouwt dist/ProductImageClassifier/ met PyInstaller (build.spec)
#   3. zipt die map naar ProductImageClassifier-<versie>-<datum>.zip in dist/
#
# Wordt aangeroepen via de snelkoppeling "Compileer build" op het bureaublad,
# of handmatig: powershell -ExecutionPolicy Bypass -File build.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== Product Image Classifier - build ==" -ForegroundColor Cyan

Write-Host "`n[1/3] Afhankelijkheden installeren..." -ForegroundColor Cyan
python -m pip install --quiet --upgrade -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install mislukt (exit $LASTEXITCODE)" }

Write-Host "`n[2/3] Compileren met PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller build.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller mislukt (exit $LASTEXITCODE)" }

Write-Host "`n[3/3] Inpakken als .zip..." -ForegroundColor Cyan
$distDir = Join-Path $PSScriptRoot "dist\ProductImageClassifier"
if (-not (Test-Path $distDir)) { throw "Verwachte map niet gevonden: $distDir" }
$stamp = Get-Date -Format "yyyy-MM-dd"
$version = (Select-String -Path (Join-Path $PSScriptRoot "config.py") -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
$zipPath = Join-Path $PSScriptRoot "dist\ProductImageClassifier-$version-$stamp.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$distDir\*" -DestinationPath $zipPath

Write-Host "`nKlaar." -ForegroundColor Green
Write-Host "Map:  $distDir"
Write-Host "Zip:  $zipPath"
Write-Host "`nDeel de .zip met collega's. Zij pakken hem uit en zetten er zelf"
Write-Host "hun OpenAI API-key in via Instellingen (of een .env naast de .exe)."

Read-Host "`nDruk op Enter om af te sluiten"
