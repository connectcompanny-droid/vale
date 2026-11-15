@echo off
rem Double-click to update dependencies and start Vale on this Windows computer (free mode).
rem The output of the setup steps goes to scripts\start_vale.log.
setlocal
cd /d "%~dp0.."
set LOG=scripts\start_vale.log
echo === %date% %time% === > "%LOG%"

where git >nul 2>&1
if not errorlevel 1 (
  echo Updating Vale... >> "%LOG%"
  git pull --ff-only >> "%LOG%" 2>&1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment... >> "%LOG%"
  py -3.12 -m venv .venv >> "%LOG%" 2>&1 || py -m venv .venv >> "%LOG%" 2>&1
)

where node >nul 2>&1
if errorlevel 1 (
  echo Installing Node.js LTS for the WhatsApp bridge... >> "%LOG%"
  winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements --silent >> "%LOG%" 2>&1
  set "PATH=%PATH%;%ProgramFiles%\nodejs"
)

echo Installing Python packages... >> "%LOG%"
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt >> "%LOG%" 2>&1

if exist "bridge\whatsapp\package.json" (
  echo Installing the WhatsApp bridge... >> "%LOG%"
  pushd bridge\whatsapp
  call npm install --omit=dev --no-audit --no-fund >> "..\..\%LOG%" 2>&1
  popd
)

echo Starting Vale... >> "%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local.ps1 >> "%LOG%" 2>&1
echo Done. Exit code %errorlevel% >> "%LOG%"
type "%LOG%"
timeout /t 15
