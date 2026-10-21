# Runs Vale on this Windows computer without Docker: API + monitor, SQLite, no Redis.
# Usage (inside the project folder, venv created):  .\scripts\run_local.ps1
# Stop: close the two windows it opens (or Ctrl+C in each).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found: create it with  py -3.12 -m venv .venv" }
if (-not (Test-Path (Join-Path $root ".env"))) { throw ".env not found: copy .env.local.example to .env and fill it" }

Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vale API'; & '$py' -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
Start-Sleep -Seconds 5
Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vale monitor'; & '$py' run_monitor.py"
Write-Host "Vale API on http://127.0.0.1:8000  (check: curl http://127.0.0.1:8000/health)"
Write-Host "Now expose it with ngrok in another window:  ngrok http --url=SEU-DOMINIO.ngrok-free.app 8000"
