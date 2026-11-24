$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
}
& $pythonPath -c "import bleak, serial"
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Bluetooth dependency installation failed.' }
}
Write-Host 'OBDWhiz is starting at http://localhost:8787. Keep this window open.'
& $pythonPath server.py --open
