# MEXC Tick Scalper — Strategy Execution Contract

This file records non-negotiable execution semantics that must be preserved across experiments, wrappers, liquidation tests, and refactors.

## Position / margin

- Futures margin mode: **ISOLATED only**.
- Total account balance is **not** the per-trade margin budget.
- Target production per-trade initial margin is **$60 USDT** for the $100-bank configuration; smaller $10–20 margins are validation-only stages.
- Per-trade initial margin remains explicitly configurable (`initial_margin_usdt`) and is capped by remaining account balance.
- Effective leverage is configurable; `0` means use the current MEXC maximum for that symbol.
- Maximum notional before book execution is `min(initial_margin_usdt, current_balance) * leverage`.
- No pyramiding, no martingale, no adding to an already-open position.

## IOC entry semantics

- Entry is a marketable **IOC LIMIT** against the current exchange book at order-arrival time.
- Frozen requested strategy target can remain `$10,000`, but the risk/margin cap is applied before walking the book.
- IOC limit cross is capped by the validated baseline (`<= 1.00 bps`) in LIVE/paper validation.
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
- Exact zero-fee universe only for **production trading eligibility**.

## Exit semantics

- Manage only actual IOC-filled quantity.
- Exit logic is event-driven; do **not** replace it with a conventional fixed take-profit/stop-loss pair unless running an explicitly separate experiment.
- **Staged positive trailing / floating take-profit is mandatory.** The previously validated winner protection locks +0.5 bps after 3 bps MFE, raises the protected floor to +2 bps after 5 bps MFE, and from 6 bps MFE switches to a monotonic ratcheting trailing stop. The stop never moves backward.
- Baseline convergence runner also retains its 1.5 bps positive trailing distance widened to at least spread where applicable; wrappers must not disable staged winner protection when using the hybrid exit policy.
- **Emergency executable-price exit is mandatory.** If the price at which the remaining position can actually be closed deteriorates beyond the emergency threshold, flatten immediately. This guard exists specifically for book blow-outs that a mid-price stop can miss.
- **Emergency adverse exit is mandatory.** If the MEXC mid moves against the position by the baseline adverse threshold, trigger irreversible exit immediately (`mid_adverse_cut`; baseline 3.0 bps after minimum hold).
- **Leader retrace emergency exit is mandatory.** If Binance, the leader, retraces against the trade by the baseline threshold, exit (`leader_retrace`; baseline 1.5 bps).
- **Residual reversal exit is mandatory.** If the residual flips to the opposite direction with sufficient magnitude, exit (`residual_reversal`; baseline 0.75 bps).
- Normal convergence remains the primary profitable exit when MEXC catches up (`mexc_catchup_convergence`).
- If expected catch-up fails to make progress, exit (`no_progress`; baseline 3000 ms with <0.5 bps progress).
- Hard maximum holding time remains a final safety fallback (`timeout`; baseline 15000 ms).
- Once any exit reason triggers, it is **irreversible**: do not cancel the exit because the signal later recovers.
- Flattening may occur in partial chunks across successive exchange book updates when the whole actually-filled position cannot be executed at once.
- Convergence / leader-retrace / residual-reversal / positive-trailing / adverse-cut logic are part of the validated baseline and must survive wrappers and liquidation instrumentation unchanged.
- External risk guards (bad market data, extreme spread/cost, liquidation/session safety) may reject an entry, but must not silently redefine the alpha model or disable these exits.

## Demo / TESTNET execution validation

- TESTNET/Demo is used to validate **real order lifecycle and execution mechanics**, not to re-prove alpha on illiquid Demo zero-fee symbols.
- Demo contracts may have non-zero fees. Do **not** reject an otherwise useful active Demo pair solely because Demo fees are non-zero.
- The primary Demo result is **net after both actual entry and actual exit fees** (`entry_fee + exit_fee`).
- A secondary `zero_fee_pnl` may be reported as a counterfactual for transferring the same observed fills to the LIVE exact-0/0 production universe, but it must never replace the actual fee-paid Demo PnL in the execution-validation verdict.
- Demo writes must remain physically restricted to `futures.testnet.mexc.com`.
- Demo entry must use real IOC semantics; Demo exit must use real reduce-only flattening and remote-position reconciliation.
- Record actual IOC POST/confirmation latency and compare remote position quantity with requested/fill quantity.
- Never stack a new position while a previous IOC result is uncertain or while any remote position remains open.

## Liquidation validation

- Liquidation tests must add realism without changing the above execution semantics.
- Track MEXC Fair/Mark Price during the open trade and compare it with the isolated-margin liquidation price.
- A trade that crosses liquidation before the strategy exit is counted as liquidated even if price later recovers.
- Missing Fair Price coverage must be reported as unknown/incomplete, never silently treated as survived.

## Regression rule

Any new runner or wrapper must have tests proving:

1. fixed isolated initial-margin cap is separate from total account balance;
2. target production margin for the $100-bank configuration defaults to $60, not the whole bank;
3. partial IOC fill cancels remainder and never tops up;
4. actual filled quantity is the only managed position quantity;
5. max leverage does not imply using the full account as margin;
6. liquidation/risk instrumentation does not alter the frozen baseline signal logic;
7. staged positive trailing remains active and monotonic;
8. adverse-cut / leader-retrace / residual-reversal / executable-price emergency exits remain active and irreversible;
9. partial flattening after an exit trigger manages only the actual remaining quantity;
10. Demo execution PnL subtracts both entry and exit fees and reports zero-fee PnL only as a secondary counterfactual.

This contract exists specifically to prevent regressions when adding account sizing, liquidation, latency, or telemetry layers.
