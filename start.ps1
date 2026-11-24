param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [switch]$Seed
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$workbenchPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $workbenchPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$dependencyHash = (Get-FileHash -LiteralPath 'requirements.txt').Hash
$dependencyMarker = Join-Path $PSScriptRoot '.venv/workbench-requirements.sha256'
if (-not (Test-Path -LiteralPath $dependencyMarker) -or
    (Get-Content -LiteralPath $dependencyMarker -Raw).Trim() -ne $dependencyHash) {
    & $workbenchPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Set-Content -LiteralPath $dependencyMarker -Value $dependencyHash -Encoding ascii
}
if ($Seed) {
    & $workbenchPython cli.py seed
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $workbenchPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
