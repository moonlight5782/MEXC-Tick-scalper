# Codex instructions for MEXC-Tick-scalper

Read `PROJECT_STATE.md` before making any architectural or strategy change. This repository is a reconstruction/research project for an old MEXC futures tick scalper that is now being prepared for guarded production use. Do not rebuild the product from scratch and do not discard existing execution/reconciliation/risk work.

## Primary objective

Reconstruct, test and improve the old bot's mechanism using measured data rather than assumptions. The strongest current hypothesis is cross-exchange Binance -> MEXC micro-lag / microspread convergence: Binance is the faster leader, MEXC is the lagger, and the bot enters on MEXC when a short-lived residual appears and exits as MEXC converges.

The product objective is positive expected value / positive net PnL with controlled drawdown. Do not optimize for trade count, uptime, or activity for their own sake. Never promise profitability or zero-loss behavior.

## LIVE / Demo execution boundary

LIVE market/account reads and LIVE web-session execution are separate concerns.

- LIVE Binance and LIVE MEXC WebSocket market data may be used for signals.
- LIVE MEXC fee/account/position reads use `MEXC_WEB_TOKEN`.
- The existing browser-session `MexcWebExecutionAdapter` is the production execution mechanism as well as the Demo mechanism; do not replace it with official Futures API merely for convenience because the strategy depends on the account's exact web/app fee state.
- Real-money writes are allowed only through the dedicated `live_production_runner.py` path and only when ALL guards are satisfied:
  - `MEXC_LIVE_WRITE=YES`
  - CLI confirmation `--confirm-live LIVE`
  - exact LIVE host `futures.mexc.com`
  - fresh account fee state for the symbol is maker=0 AND taker=0
  - no ambiguous/open bot state before a new entry
- `start_live.bat` is the explicit real-money launcher. Do not make `start_demo.bat`, scanners, shadow runners, or research scripts send LIVE orders.
- Demo/Testnet writes still use `MEXC_DEMO_WEB_TOKEN` and remain isolated from LIVE.
- Do not print or commit tokens, cookies, `.env`, credentials, or session secrets.

## Fee requirement

The intended real strategy requires exact current MEXC account fees `maker=0` AND `taker=0` for the traded symbol.

For LIVE strategy decisions:
- missing/stale fee data is NOT tradable;
- unknown fee is never treated as zero;
- fee state is refreshed in the background so fee REST reads are not placed on the signal-critical path;
- if the fee gate becomes stale/nonzero while a bot-owned position is open, the production runner exits rather than opening or holding blindly.

Demo/Testnet fee behavior is simulator-specific and must not redefine the LIVE strategy universe.

## Current strategy direction

The production strategy is event-driven Binance -> LIVE MEXC microspread convergence.

Current intended LIVE pipeline:

1. Discover current `LIVE MEXC exact 0/0 fee ∩ Binance USD-M` symbols.
2. Subscribe continuously to Binance USD-M `bookTicker` and MEXC LIVE full-depth WebSocket data.
3. Maintain the normal cross-exchange basis `log(Binance_mid / MEXC_mid)` with a robust rolling baseline.
4. Detect short residual excursions around that baseline, including sub-1-bps excursions; do not require a large 4-10 bps impulse unless measured data justifies it.
5. Require the residual to remain economically executable after current LIVE MEXC bid/ask spread plus a small net-edge buffer.
6. Use hysteresis/rearm so one persistent excursion cannot generate repeated entries.
7. Re-evaluate the candidate from the newest local WS state immediately before order submit.
8. Build a marketable IOC directly from cached LIVE MEXC best bid/ask and contract tick size; do not put a REST market-price lookup before the IOC.
9. Accept partial IOC fill and do not market-top-up the entry.
10. Manage the actual filled position and close primarily on convergence, with reversal/adverse/timeout/positive-trailing/data-stale/fee-gate exits.
11. Verify reduce-only close actually removed the exact bot position; retry bounded residual flattening if necessary.

`start_demo.bat` and Demo/shadow modes remain regression/research tools. Do not delete them.

## Latency principles

- Prefer WebSocket/local state over REST on the signal-critical path.
- Entry detection is event-driven rather than a fixed polling loop.
- REST may be used for discovery, background fee refresh, contract metadata, order/position reconciliation, and non-critical accounting.
- Measure latency instead of claiming millisecond performance.
- Do not wait for full position-visibility reconciliation before beginning risk/exit monitoring if an IOC fill is already confirmed; provisional state is acceptable when safely reconciled.
- When changing the critical path, retain telemetry for signal -> IOC submit/result -> position visibility and exit decision -> close result where practical.

## Execution behavior to preserve

Historical reconstruction strongly suggests:

large IOC limit -> accept partial fill -> cancel/unfilled remainder -> do NOT market-top-up entry -> manage actual fill -> reduce-only exit.

Do not replace this with unconditional market entry without measured evidence.

## Positive trailing requirement

The user specifically wants the floating stop to protect PROFIT, not intentionally ratchet backward into a loss after meaningful executable profit exists.

Current staged design:
- MFE >= +3 bps: profit floor about +0.5 bps
- MFE >= +5 bps: floor about +2.0 bps
- above mature activation: ratcheting trailing behind MFE
- the stop may only tighten, never loosen

Actual exchange fills can still slip through a software stop, so never claim guaranteed realized positive PnL.

## Risk controls

Keep or improve:
- explicit real-money unlock and typed confirmation
- maximum session loss / drawdown halt
- maximum simultaneous bot position = 1 unless explicitly redesigned and tested
- position timeout
- adverse cut
- fee freshness gate
- market-data freshness exit while a bot position is open
- startup refusal when unrelated LIVE positions already exist, unless the user explicitly opts into coexistence
- exact `positionId` reduce-only reconciliation after close
- shutdown/cancellation emergency close for bot-owned position

High leverage magnifies tiny errors. Do not increase leverage as a substitute for proving edge. The current production runner uses the contract maximum only up to the user-supplied leverage cap and keeps notional sizing separate from leverage.

## Testing and Git workflow

Before considering work complete:

```bash
pip install -e . pytest
pytest -q
```

CI is `.github/workflows/ci.yml` on Python 3.11. Do not say tests pass unless they actually passed locally or in CI.

Prefer small, reviewable changes. Preserve existing working modes for comparison rather than deleting research code prematurely. Do not commit generated logs, credentials, or personal `.env` files.

## Important files

- `PROJECT_STATE.md` - project handoff/current state; read first.
- `src/mexc_tick_scalper/live_production_runner.py` - guarded real-money Binance -> LIVE MEXC microspread execution runner.
- `start_live.bat` - explicit real-money launcher; never make Demo/scanner launchers silently call it.
- `src/mexc_tick_scalper/microspread.py` - current microspread/baseline/hysteresis model.
- `src/mexc_tick_scalper/microspread_feed.py` - event-driven Binance/MEXC LIVE market feeds.
- `src/mexc_tick_scalper/demo_microspread_test.py` - Demo/Testnet execution regression/research runner.
- `src/mexc_tick_scalper/demo_live_launcher.py` - interactive launcher used by `start_demo.bat`.
- `src/mexc_tick_scalper/lead_lag.py` - older lead-lag model retained for research/comparison.
- `src/mexc_tick_scalper/demo_multi_lead_lag_test.py` - older continuous large-lag Demo mode retained for comparison.
- `src/mexc_tick_scalper/live_lead_lag_scan.py` - broad LIVE 0/0 research scanner.
- `src/mexc_tick_scalper/live_zero_fee_universe.py` - LIVE account exact-zero-fee discovery.
- `src/mexc_tick_scalper/web_execution.py` - browser-session execution adapter used by Demo and guarded LIVE production.
- `src/mexc_tick_scalper/demo_hybrid_test.py` - IOC reconciliation/flatten/execution infrastructure.
- `src/mexc_tick_scalper/hybrid_strategy.py` - earlier MEXC-only microstructure strategy and trailing logic.

When uncertain, inspect current code and tests before changing architecture.