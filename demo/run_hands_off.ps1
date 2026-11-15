# Windows PowerShell wrapper → demo/run_hands_off.py
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
  & $VenvPy (Join-Path $Root "demo\run_hands_off.py") @args
} else {
  & python (Join-Path $Root "demo\run_hands_off.py") @args
}
exit $LASTEXITCODE
