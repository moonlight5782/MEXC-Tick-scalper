# Codex instructions for MEXC-Tick-scalper

Read `PROJECT_STATE.md` before making any architectural or strategy change. This repository is a reconstruction/research project for an old MEXC futures tick scalper. Do not rebuild the product from scratch and do not discard existing execution/reconciliation/risk work.

## Primary objective

Reconstruct, test and improve the old bot's mechanism using measured data rather than assumptions. The strongest current hypothesis is cross-exchange Binance -> MEXC micro-lag / microspread convergence: Binance is the faster leader, MEXC is the lagger, and the bot enters on MEXC when a short-lived residual appears and exits as MEXC converges.

The product objective is positive expected value / positive net PnL with controlled drawdown. Do not optimize for trade count, uptime, or activity for their own sake. Never promise profitability or zero-loss behavior.

## Hard safety boundary

- PRIVATE LIVE MEXC order/position writes must remain disabled.
- LIVE Binance and LIVE MEXC market data may be used for signals and research.
- LIVE MEXC account data may be read with `MEXC_WEB_TOKEN` only through `write_enabled=False` adapters.
- Actual order writes are allowed only on MEXC Demo/Testnet through `MEXC_DEMO_WEB_TOKEN` with `MEXC_DEMO_WRITE=YES`.
- Do not silently change this boundary.
- Do not print or commit tokens, cookies, `.env`, credentials, or session secrets.

## Fee requirement

The intended real strategy requires exact current MEXC account fees `maker=0` AND `taker=0` for the traded symbol. The LIVE account currently exposes many exact 0/0 pairs; Demo/Testnet has a much smaller symbol universe. Demo fee behavior is simulator-specific and must not redefine the LIVE strategy universe.

For LIVE strategy decisions, treat missing/stale fee data as NOT tradable. Never treat unknown fees as zero.

## Current strategy direction

`start_demo.bat` / `demo_live_launcher.py` should run the event-driven microspread Demo mode, not the older rare-large-lag mode.

Current intended pipeline:

1. Discover `LIVE MEXC exact 0/0 fee ∩ Binance USD-M ∩ MEXC Demo` exact-symbol intersection.
2. Subscribe continuously to Binance USD-M `bookTicker` and MEXC LIVE depth/book WebSocket data.
3. Maintain the normal cross-exchange basis `log(Binance_mid / MEXC_mid)` with a robust rolling baseline.
4. Detect short residual excursions around that baseline, including sub-1-bps excursions; do not require a large 4-10 bps impulse.
5. Require the residual to be economically executable after the current LIVE MEXC spread plus a small net-edge buffer.
6. Use hysteresis/rearm so one persistent excursion cannot generate repeated entries.
7. Execute only on MEXC Demo/Testnet.
8. Manage the Demo position using LIVE MEXC executable bid/ask economics plus convergence/reversal/adverse-cut/positive trailing logic.

Do not reintroduce an arbitrary large `edge >= 4 bps` or `Binance move >= 1 bps` gate into the microspread path without data proving it is necessary.

## Latency principles

- Prefer WebSocket/local state over REST on the signal-critical path.
- Event-driven wake-up is preferred to polling loops for entry detection.
- REST may be used for discovery, fee refresh, Demo execution details, and non-critical reconciliation.
- Measure latency instead of claiming millisecond performance.
- When changing the critical path, add telemetry for signal timestamp -> Demo price lookup -> IOC request/response -> position visibility when useful.

## Execution behavior to preserve

Historical reconstruction strongly suggests: large IOC limit -> accept partial fill -> cancel/unfilled remainder -> do NOT market-top-up entry -> manage actual fill -> reduce-only market exit.

Current Demo execution also includes late position-visibility reconciliation because MEXC Testnet can report an IOC fill before the position appears remotely. Do not remove this without a replacement that prevents duplicate/stacked entries.

## Positive trailing requirement

The user specifically wants the floating stop to protect PROFIT, not intentionally ratchet backward into a loss after a meaningful executable profit exists.

Current staged design:
- MFE >= +3 bps: profit floor about +0.5 bps
- MFE >= +5 bps: floor about +2.0 bps
- above mature activation: ratcheting trailing behind MFE
- the stop may only tighten, never loosen

Actual exchange fills can still slip through a software stop, so never claim guaranteed realized positive PnL.

## Risk controls

Keep or improve:
- maximum session loss / drawdown halt
- maximum simultaneous positions = 1 unless explicitly redesigned and tested
- position timeout
- adverse cut
- fee freshness gate
- startup/shutdown Demo flatten/reconciliation

High leverage magnifies tiny errors. Do not increase leverage as a substitute for proving edge.

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
- `src/mexc_tick_scalper/microspread.py` - current microspread/baseline/hysteresis model.
- `src/mexc_tick_scalper/microspread_feed.py` - event-driven Binance/MEXC LIVE market feeds.
- `src/mexc_tick_scalper/demo_microspread_test.py` - current LIVE-signal -> Demo execution runner.
- `src/mexc_tick_scalper/demo_live_launcher.py` - interactive launcher used by `start_demo.bat`.
- `src/mexc_tick_scalper/lead_lag.py` - older lead-lag model retained for research/comparison.
- `src/mexc_tick_scalper/demo_multi_lead_lag_test.py` - older continuous large-lag Demo mode retained for comparison.
- `src/mexc_tick_scalper/live_lead_lag_scan.py` - broad LIVE 0/0 research scanner.
- `src/mexc_tick_scalper/live_zero_fee_universe.py` - LIVE account exact-zero-fee discovery.
- `src/mexc_tick_scalper/web_execution.py` - web/Testnet execution adapter and safety boundaries.
- `src/mexc_tick_scalper/demo_hybrid_test.py` - IOC reconciliation/flatten/execution infrastructure.
- `src/mexc_tick_scalper/hybrid_strategy.py` - earlier MEXC-only microstructure strategy and trailing logic.

When uncertain, inspect current code and tests before changing architecture.