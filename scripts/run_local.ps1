# Runs Vale on this Windows computer without Docker: API + monitor, SQLite, no Redis.
# Usage (inside the project folder, venv created):  .\scripts\run_local.ps1
# Stop: close the two windows it opens (or Ctrl+C in each).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found: create it with  py -3.12 -m venv .venv" }
if (-not (Test-Path (Join-Path $root ".env"))) { throw ".env not found: copy .env.local.example to .env and fill it" }

# Operator panel: create ADMIN_TOKEN on first run and leave a shortcut that opens the panel.
$envFile = Join-Path $root ".env"
$adminToken = ([regex]::Match((Get-Content $envFile -Raw), "(?m)^\s*ADMIN_TOKEN\s*=\s*(\S+)")).Groups[1].Value
if (-not $adminToken) {
    $adminToken = (& $py -c "import secrets; print(secrets.token_hex(24))").Trim()
    [IO.File]::AppendAllText($envFile, "`r`nADMIN_TOKEN=$adminToken`r`n")
}
[IO.File]::WriteAllText((Join-Path $root "Vale painel.url"), "[InternetShortcut]`r`nURL=http://127.0.0.1:8000/admin?token=$adminToken`r`n")

Push-Location $root
& $py scripts\migrate.py
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Database migration failed (see the message above)" }
Pop-Location

# Free WhatsApp bridge (bridge/whatsapp) when WHATSAPP_PROVIDER=evolution and Evolution points at this computer.
$envText = Get-Content (Join-Path $root ".env") -Raw
$bridge = Join-Path $root "bridge\whatsapp"
if ($envText -match "(?m)^\s*WHATSAPP_PROVIDER\s*=\s*evolution" -and $envText -notmatch "(?m)^\s*EVOLUTION_BASE_URL\s*=\s*https?://(?!127\.0\.0\.1|localhost)") {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js 20+ is needed for the WhatsApp bridge: winget install OpenJS.NodeJS.LTS" }
    if (-not (Test-Path (Join-Path $bridge ".env"))) { throw "bridge\whatsapp\.env not found: copy bridge\whatsapp\.env.example and fill it" }
    if (-not (Test-Path (Join-Path $bridge "node_modules"))) { Push-Location $bridge; npm install --omit=dev; Pop-Location }
    Start-Process powershell -WorkingDirectory $bridge -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vale WhatsApp'; node index.mjs"
    Write-Host "WhatsApp bridge on http://127.0.0.1:8081  (first time: open http://127.0.0.1:8081/qr and scan it with the phone)"
}

Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vale API'; & '$py' -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
Start-Sleep -Seconds 5
Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vale monitor'; & '$py' run_monitor.py"
Write-Host "Vale API on http://127.0.0.1:8000  (check: curl http://127.0.0.1:8000/health)"
Write-Host "Operator panel: double-click 'Vale painel' in the project folder."
Write-Host "Website chat on this computer: http://127.0.0.1:8000/chat"
# Public address for the website chat (and for UAZAPI/Stripe webhooks): set NGROK_DOMAIN in .env.
$ngrokDomain = ([regex]::Match($envText, "(?im)^\s*NGROK_DOMAIN\s*=\s*(\S+)")).Groups[1].Value
if ($ngrokDomain -and (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vale public address'; ngrok http --domain=$ngrokDomain 8000"
    Write-Host "Public chat: https://$ngrokDomain/chat"
} else {
    Write-Host "To open the chat to clients, set NGROK_DOMAIN=your-domain.ngrok-free.app in .env (free ngrok account) and start again."
}
