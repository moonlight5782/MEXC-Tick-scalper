# MEXC Tick Scalper — Frozen Baseline V1 Contract

This document exists to prevent experiments, Demo wrappers, liquidation instrumentation, or old-bot reconstruction work from silently changing the validated strategy.

## Source of truth

- The strategy source of truth is `src/mexc_tick_scalper/baseline_v1.py` together with `prelive_persistent_ioc_shadow_v2.py`.
- `baseline_v1.py` was frozen after the successful 100-closed-trade LIVE-paper arrival-book IOC validation.
- Do not copy parameters from the older reconstructed bot, older hybrid/Demo strategies, or later risk experiments into baseline v1.
- The old approximately-$60 account/bot reconstruction is historical evidence only. **$60 is not a baseline-v1 production margin rule.**

## Pair selection

A symbol is eligible for **production baseline-v1 trading** only when all of the following hold:

1. It is currently available on MEXC Futures and cross-listed on Binance USD-M so Binance can act as the leader.
2. The LIVE MEXC account currently confirms exact maker=0 and taker=0 for the contract.
3. It passes the persistent-lag profile built from the measured lifetime diagnostic:
   - at least 4 observed signals;
   - median lag lifetime >= 300 ms;
   - at least 50% of observed lag signals survive the measured execution RTT;
   - median signal-strength ratio >= 1.50x.
4. Demo/Testnet activity or Demo fees must never redefine this production eligibility rule.

## Frozen signal / entry rules

The exact values in `baseline_v1.py` remain authoritative. Key rules include:

- requested target notional: $10,000 before IOC partial-fill reality;
- residual >= 8 bps;
- signal strength >= 3.0x;
- Binance move >= 1.0 bps and leader advantage >= 1.0 bps;
- lead ratio >= 1.35;
- confirmation: 2 updates and >=15 ms;
- residual retention at arrival >=60%;
- Binance impulse retention at arrival >=75%;
- executable edge after spread >=2 bps;
- IOC cross <=1.0 bps;
- average entry slippage <=1.0 bps;
- minimum actually-filled notional $50;
- residual must cover actual round-trip execution cost by both +2 bps and 1.5x;
- Binance quote age <=300 ms;
- MEXC arrival book age <=750 ms.

## IOC execution semantics

- Entry uses the current MEXC order book at simulated/real order-arrival time.
- IOC partial fill is accepted.
- The unfilled remainder is cancelled immediately.
- **Never top up, chase, average, pyramid, or send another order merely to complete the original $10,000 request.**
- Position management uses only the actually filled quantity.

Example: request $10,000; only $2,000 is executable inside the IOC limit; manage $2,000 and cancel the remaining $8,000.

## Frozen exits

Baseline-v1 exit logic remains unchanged:

- `mid_adverse_cut`: 3.0 bps adverse MEXC-mid movement after the 50 ms minimum hold;
- `leader_retrace`: 1.5 bps Binance retrace against the position;
- `residual_reversal`: opposite residual >=0.75 bps;
- `mexc_catchup_convergence`: MEXC catches up and residual converges;
- `no_progress`: after 3000 ms with <0.5 bps progress;
- positive trailing: 1.5 bps distance, widened to at least the current spread;
- hard timeout: 15000 ms.

Once an exit reason fires it is irreversible. Flattening may happen in partial chunks on successive MEXC book updates and applies only to remaining actually-filled quantity.

Older staged-hybrid trailing experiments are historical and must not silently replace baseline-v1 `PositiveTrailing` unless a separately named baseline-v2 experiment is created and validated.

## Demo / TESTNET validation

We already validated baseline-v1 alpha and arrival-book behavior on LIVE Binance + LIVE MEXC market data. Demo is therefore used to check exchange execution mechanics.

For the dedicated Demo execution test:

- the **production-only** exact-0/0 fee gate and historical persistent-profile gate may be bypassed solely for choosing a usable Testnet test symbol;
- the selected test symbol must exist on MEXC Testnet, LIVE MEXC, and Binance USD-M so the same Binance->MEXC lead-lag signal can still be computed;
- when no explicit symbol is supplied, choose an actually active Testnet symbol rather than a dead zero-fee contract;
- once the symbol is chosen, all frozen BASELINE_V1 signal/RTT/retention/IOC/slippage/executable-cost/exit thresholds remain unchanged;
- Testnet order writes must remain physically restricted to `futures.testnet.mexc.com`;
- use real IOC entry and reduce-only/position-safe flattening;
- accept actual Demo partial fills and never top up the remainder;
- record actual submit/ACK/fill latency and remote-position reconciliation;
- Demo fees may be non-zero; report PnL after subtracting **both opening and closing commissions**;
- also report a separate zero-fee counterfactual for comparison with the production exact-0/0 universe;
- Demo results validate execution behavior and should not be used to redefine production pair eligibility.

## Regression rule

Any Demo or liquidation wrapper must prove that it imports/applies `BASELINE_V1` rather than re-declaring a similar-looking set of thresholds. If a wrapper needs different thresholds, that is a new experiment and must not be labeled baseline v1.
