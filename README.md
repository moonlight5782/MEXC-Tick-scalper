# MEXC Tick Scalper

Adaptive zero-fee futures tick-scalping research bot for MEXC.

## Core rules

- Trade only when **effective maker fee = 0 and effective taker fee = 0** on real trading.
- If a fee appears, do not open new trades; keep monitoring the symbol and re-enable only after 0% maker + 0% taker is confirmed again.
- Market selection is dynamic: symbols are ranked by current microstructure and shadow/replay performance, not hard-coded coin names.
- Entries use short-term momentum and IOC execution semantics.
- No pyramiding or martingale.
- Exit follows the favorable extreme and closes on the first configured adverse tick reversal.
- Unknown fee status, stale market data, disconnected market feed, or unknown position state blocks new entries.

## Current status

The repository contains scanner, tick recorder, shadow replay, walk-forward backtest, adaptive parameter search, fee/risk gates, paper execution, web-session execution, and a dedicated MEXC Futures Demo Trading mode.

Real trading remains disabled by default. Demo mode is hard-bound to `futures.testnet.mexc.com` and rejects live MEXC hosts.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e .
cp config.example.yaml config.yaml
mexc-scalper scan --config config.yaml
```

Record fresh ticks and run walk-forward validation:

```bash
mexc-scalper record --symbol BTC_USDT --seconds 3600 --output data/BTC_USDT.csv --config config.yaml
mexc-scalper backtest --symbol BTC_USDT --input data/BTC_USDT.csv --config config.yaml
```

## MEXC Demo Trading

MEXC Demo Trading uses the separate testnet environment at `futures.testnet.mexc.com`.

Copy `.env.example` to `.env` and set `MEXC_DEMO_WEB_TOKEN` from an authenticated Demo Trading browser session. Demo credentials are intentionally separate from live credentials.

Read-only session check:

```bash
mexc-scalper demo-check --symbol BTC_USDT
```

One small simulated IOC entry followed immediately by a market flatten:

```bash
mexc-scalper demo-roundtrip \
  --symbol BTC_USDT \
  --side long \
  --notional-usdt 10 \
  --leverage 5 \
  --confirm-demo-order
```

`demo-roundtrip` refuses to place an order unless `--confirm-demo-order` is supplied, refuses any existing position on the selected symbol, and the Demo configuration refuses any host other than `futures.testnet.mexc.com`.

## Safety

`live_enabled` is `false` by default. Secrets, cookies, API keys, session IDs and `u_id`/WEB token values must never be committed to GitHub.

The intended rollout is: walk-forward backtest -> fresh shadow/replay -> Demo Trading -> minimal real exposure -> scale only after live results match the validated strategy.

This software is experimental and does not guarantee profit.
