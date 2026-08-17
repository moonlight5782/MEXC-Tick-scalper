# MEXC Tick Scalper — Strategy Execution Contract

This file records non-negotiable execution semantics that must be preserved across experiments, wrappers, liquidation tests, and refactors.

## Position / margin

- Futures margin mode: **ISOLATED only**.
- Total account balance is **not** the per-trade margin budget.
- Per-trade initial margin is an explicit configurable dollar amount (`initial_margin_usdt`) capped by remaining account balance.
- Effective leverage is configurable; `0` means use the current MEXC maximum for that symbol.
- Maximum notional before book execution is `min(initial_margin_usdt, current_balance) * leverage`.
- No pyramiding, no martingale, no adding to an already-open position.

## IOC entry semantics

- Entry is a marketable **IOC LIMIT** simulation against the LIVE MEXC order book at simulated order-arrival time.
- Frozen requested strategy target can remain `$10,000`, but the risk/margin cap is applied before walking the book.
- IOC limit cross is capped by the validated baseline (`<= 1.00 bps`).
- Only price levels inside the IOC limit may fill.
- **Partial fill is accepted.**
- **The unfilled remainder is cancelled immediately and is NEVER topped up.**
- Position management and PnL use only the actually filled quantity/notional.
- Do not wait for more liquidity merely to complete the requested amount.

Example: if the strategy requests $10,000 notional but only $2,000 is executable inside the IOC limit, open $2,000 and cancel the remaining $8,000. Do not chase or refill.

## Alpha / entry baseline

Preserve the frozen validated baseline unless an experiment is explicitly labeled as a strategy change:

- LIVE Binance + LIVE MEXC market data.
- Residual >= 8 bps.
- Signal strength >= 3.0x.
- Remaining executable edge must cover actual partial-fill roundtrip cost by +2 bps and 1.5x.
- Arrival-time LIVE MEXC book; no artificial extra depth-update wait.
- Exact zero-fee universe only for trading eligibility.

## Exit semantics

- Manage only actual IOC-filled quantity.
- Convergence / leader-retrace / residual-reversal logic remains part of the validated baseline.
- Do not transform the strategy into conventional fixed TP/SL trading without an explicitly separate experiment.
- External risk guards (bad market data, extreme spread/cost, liquidation/session safety) may reject an entry, but must not silently redefine the alpha model.

## Liquidation validation

- Liquidation tests must add realism without changing the above execution semantics.
- Track MEXC Fair/Mark Price during the open trade and compare it with the isolated-margin liquidation price.
- A trade that crosses liquidation before the strategy exit is counted as liquidated even if price later recovers.
- Missing Fair Price coverage must be reported as unknown/incomplete, never silently treated as survived.

## Regression rule

Any new runner or wrapper must have tests proving:

1. fixed isolated initial-margin cap is separate from total account balance;
2. partial IOC fill cancels remainder and never tops up;
3. actual filled quantity is the only managed position quantity;
4. max leverage does not imply using the full account as margin;
5. liquidation/risk instrumentation does not alter the frozen baseline signal logic.

This contract exists specifically to prevent regressions when adding account sizing, liquidation, latency, or telemetry layers.
