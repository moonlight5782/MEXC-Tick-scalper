@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found: .venv
  pause
  exit /b 1
)

set "LOG=%~1"
if "%LOG%"=="" (
  set /p LOG=Path to saved paper log: 
)

set /p BAL=Starting balance USDT [100]: 
if "%BAL%"=="" set "BAL=100"

set /p MARGIN=Initial margin budget per trade USDT [50]: 
if "%MARGIN%"=="" set "MARGIN=50"

set /p LEV=Leverage [0 = current MEXC maximum per symbol]: 
if "%LEV%"=="" set "LEV=0"

.venv\Scripts\python.exe -m mexc_tick_scalper.margin_liquidation_replay "%LOG%" ^
  --balance-usdt %BAL% ^
  --initial-margin-usdt %MARGIN% ^
  --leverage %LEV%

pause
