@echo off
setlocal
cd /d %~dp0
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m mexc_tick_scalper.demo_cleanup
) else (
  python -m mexc_tick_scalper.demo_cleanup
)
pause
