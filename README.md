# MEXC Tick Scalper

Adaptive zero-fee futures tick-scalping research bot for MEXC.

## Core rules

- Trade only when **effective maker fee = 0 and effective taker fee = 0**.
- Market selection is dynamic: symbols are ranked by current microstructure and shadow/replay performance, not hard-coded coin names.
- Entries use short-term momentum and IOC-style execution semantics.
- No pyramiding or martingale.
- Exit follows the favorable extreme and closes on the first configured adverse tick reversal.
- Unknown fee status, stale market data, disconnected market feed, or unknown position state blocks new entries.

## Current status

This repository starts with a working **scanner + tick buffer + shadow replay + adaptive parameter search + risk/fee gates**. Live order execution is isolated behind an execution adapter and is disabled by default.

Public market data uses MEXC Futures public endpoints/WebSocket. MEXC currently applies a separate fee schedule to official Futures API trading, so the strategy must never assume API execution is zero-fee.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e .
cp config.example.yaml config.yaml
mexc-scalper scan --config config.yaml
```

Run a single-symbol live feed + shadow engine:

```bash
mexc-scalper shadow --symbol BTC_USDT --config config.yaml
```

## Safety

`live_enabled` is `false` by default. Secrets, cookies, API keys, session IDs and `u_id` values must never be committed to GitHub.

This software is experimental and does not guarantee profit. Validate in shadow mode and with minimal real exposure before scaling.
