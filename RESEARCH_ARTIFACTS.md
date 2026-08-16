# Research artifacts policy

The repository keeps source code, tests, handoff documentation, and only a small set of research datasets that are useful for reproducing current conclusions.

## Curated datasets intentionally kept

- `binance_impulse_fast_100_excursions_20260815.csv` — Demo excursion / execution-latency telemetry used by the current handoff.
- `binance_impulse_live_shadow_instant_100_20260815.csv` — completed zero-delay LIVE read-only control.
- `binance_impulse_live_shadow_current_rtt_1bps_100_20260816.csv` — completed 100-trade LIVE read-only current-RTT control.
- `depth_scaled_current_rtt_1bps_100_20260816.csv` — completed depth-aware / equity-scaling 100-trade control.
- `live_lead_lag_1786709118.csv` — early broad LIVE zero-fee lead-lag snapshot retained as a reconstruction reference.
- `uni_deals.json`, `uni_live_history.json`, `uni_min1.csv`, `bch_min1.csv` — small historical/reconstruction inputs retained because they may still be useful when comparing the old bot.

## Generated files not kept in Git

Raw logs, error logs, residual streams, smoke/replay CSVs, temporary patch files, packaging metadata, and local test scratch are intentionally ignored. They can be regenerated and should not be committed by default.

If a future experiment becomes a reproducibility baseline, add only its compact result/summary dataset deliberately and document why it is being retained.

This cleanup is repository hygiene only. It must not change trading logic, strategy parameters, LIVE/Demo safety boundaries, or test behavior.
