@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Reference: 8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5
REM Frozen BASELINE_V1 + measured private RTT + arrival-time LIVE book.
REM Original persistent profile and LIVE exact maker=0/taker=0 eligibility are preserved.
REM Testnet is only the final same-symbol execution intersection.
REM Single real Testnet IOC; partial fill is managed as-is; NO top-up/chase/retry.
REM Independent risk layer: leverage/liquidation/ROE/API fail-safe. No LIVE order writes.

.venv\Scripts\python.exe -m mexc_tick_scalper.testnet_frozen_latency_runner ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --risk-max-leverage 10 ^
  --min-liq-distance-bps 500 ^
  --emergency-liq-distance-bps 300 ^
  --max-adverse-roe-pct 8 ^
  --risk-poll-ms 200 ^
  --max-risk-poll-failures 5 ^
  --max-testnet-basis-bps 50

pause
