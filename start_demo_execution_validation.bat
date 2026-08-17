@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

if "%~1"=="" (
  echo Usage: start_demo_execution_validation.bat SYMBOL
  echo Example: start_demo_execution_validation.bat BTC_USDT
  echo.
  echo Choose an ACTIVE Demo symbol; Demo fees are allowed and BOTH entry+exit fees are deducted.
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m mexc_tick_scalper.demo_execution_validation_v1 ^
  --symbol %~1 ^
  --session-seconds 1800 ^
  --max-cycles 20 ^
  --target-margin-usdt 60 ^
  --leverage 0 ^
  --emergency-executable-cut-bps 3.0

pause
