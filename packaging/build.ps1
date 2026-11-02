# Crea dist\Mikoshi\Mikoshi.exe y, si Inno Setup 6 esta instalado, tambien el instalador.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --name Mikoshi `
    --icon mikoshi\assets\mikoshi.ico `
    --add-data "mikoshi\assets;mikoshi\assets" `
    --paths . `
    --distpath dist --workpath build packaging\mikoshi_main.py
if ($LASTEXITCODE) { throw "PyInstaller falló" }

$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($iscc) {
    & $iscc packaging\mikoshi.iss
    if ($LASTEXITCODE) { throw "Inno Setup falló" }
} else {
    Write-Host "Inno Setup no encontrado: solo se ha generado dist\Mikoshi."
}
