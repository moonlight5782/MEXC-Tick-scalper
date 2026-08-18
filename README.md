# MEXC Tick Scalper

One active product line exists: `canonical-latency-arb-v1`.

Frozen alpha reference: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`; `372c3b2` is the known-good 100-trade paper source. `BASELINE_V1` is immutable in-place.

## Install

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . pytest
copy .env.example .env
```

Put browser-session tokens only in local `.env`; never commit them.

## 1. Validate LIVE alpha, read-only

```powershell
.\start_canonical_shadow.bat
```

The launcher is clean-clone capable. If no persistent-lag profile exists locally it first collects a current read-only LIVE Binance/MEXC lifetime sample using BASELINE_V1-compatible settings, refuses to weaken thresholds when no eligible pair exists, then starts the canonical end-to-end shadow.

The shadow uses LIVE Binance as leader, LIVE MEXC as follower, current exact 0/0 fee eligibility, frozen arrival-book IOC economics, and continuously measured current latency. It sends no orders.

## 2. Calibrate real Testnet execution

Set a Testnet browser token in `.env` and explicitly set `MEXC_DEMO_WRITE=YES`, then run:

```powershell
.\start_canonical_testnet_calibrator.bat
```

This sends MEXC Testnet orders only. It measures actual IOC submit->confirm->position-visible and close submit->confirm->flat timings, actual partial fills and fees. Testnet PnL is not used to judge the LIVE Binance->MEXC alpha.

## Source of truth

Read `CANONICAL.md` before changing strategy or execution architecture. Historical branches are research archives only; do not merge their strategy semantics wholesale into canonical.

Real-money LIVE order writes are not part of the canonical workflow and are not considered validated.
