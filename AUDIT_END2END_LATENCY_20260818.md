# End-to-end latency audit — 2026-08-18

## Decision

The product must not continue from the wrapper stack built after `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`.

This branch starts exactly from that frozen point and reconstructs one cohesive product path from two previously validated lines of work:

1. **Persistent Binance->MEXC alpha / arrival-book partial IOC** from `prelive_persistent_ioc_shadow_v2.py`.
2. **Separate entry and exit latency scheduling** from the earlier latency-aware `live_binance_impulse_shadow.py` line (`fa932d7e`, `80fb1fe8`, `e0b4e872`, `1ce77898`, `304b2bbf`).

No proxy symbols, stdout mirrors, Testnet follower substitution, fallback strategy universe, monkeypatch execution wrappers, or artificial pre-submit sleep belong in the production strategy.

## What the proven 100-trade result actually proves

Commit `372c3b286eb82aa4b87d806999f8db47173a2b3e` produced the known 100-trade result and `8a0bc60` froze its settings without changing the core V2 runner.

The V2 runner correctly models:

- LIVE Binance leader + LIVE MEXC follower.
- persistent-pair selection;
- exact LIVE account maker=0/taker=0 universe;
- adaptive lead/lag gate;
- signal persistence / residual retention;
- an entry delay based on measured private read RTT;
- the LIVE MEXC book at simulated entry-arrival time;
- one marketable partial IOC with no top-up;
- depth/slippage and immediate executable round-trip cost;
- sticky exit priority and depth-aware partial flattening.

However, it does **not** model end-to-end execution. After an exit condition fires, it starts consuming the current LIVE book without a separate exit-decision-to-order-arrival delay. Thus the 76% WR / PF 13.61 result is evidence of a strong alpha candidate under an optimistic exit model, not proof that a real web order path can reproduce those 90–134ms median holds.

## Real latency evidence already present in repository history

The earlier Demo/latency line measured materially slower execution than the later V2 paper model assumed.

Historical project state records approximately:

- Demo IOC confirmation median/p95: 642.9ms / 1766.6ms.
- Demo position visibility median/p95: 1041.6ms / 5049.5ms.
- An earlier LIVE read-only current-RTT shadow generated 100 rows with separate `signal_to_fill_ms` and `exit_decision_to_fill_ms`.
- The zero-delay LIVE shadow showed strong signal-time edge, but repository documentation explicitly warned that it did not prove order-arrival feasibility.

A read-only GET `/positions` RTT is a network-health proxy only. It is not an IOC submit/fill latency. Dividing such RTT by two and treating it as entry or exit execution latency is not acceptable for product economics.

## Concrete regressions found

### 1. Exit latency disappeared in the persistent V2 line

Entry latency survived the evolution; exit latency did not. This is the largest methodological gap in the supposedly known-good paper result.

### 2. `LiveRttProbe` class regression

Commit `c196f58ea6cc78cb0058556f5a2c68e9284ddf77` inserted `_entry_latency_allowed()` at module scope before the remaining `LiveRttProbe` methods. `start`, `close`, and `_run` consequently became nested inside that helper after its `return`, rather than class methods. The current `8a0bc60` snapshot therefore contains a broken layout even though the earlier `304b2bb` snapshot was correct.

This demonstrates that later commits cannot be assumed better merely because they are newer.

### 3. Testnet/LIVE market mixing produced meaningless strategy PnL

A later frozen Testnet attempt used LIVE MEXC to decide entry/exit but Testnet MEXC to hold the actual position. One observed ARB trade entered with roughly 13.3bps Testnet-vs-LIVE basis and then produced roughly -13.3bps zero-fee Testnet PnL when the LIVE exit fired. That is market mismatch, not a valid test of the production alpha.

### 4. Testnet-follower replacement changes the strategy

Using Binance LIVE as leader and MEXC Testnet as follower+execution is internally consistent for testing Testnet market mechanics, but it no longer validates the proven LIVE MEXC lag strategy. Testnet lag lifetime, spread, liquidity and basis are different data-generating processes.

### 5. Inconsistent latency definitions in the latest product branch

The abandoned `latency-arb-product-v1` bootstrapped entry/exit from private GET RTT / 2, then updated entry latency from POST response time while updating exit latency from a full flatten/reconciliation duration. Those quantities are not comparable and must not be added into one economic budget.

### 6. Testnet fees were not part of the V2 zero-fee cost gate

`immediate_roundtrip_cost_bps()` models executable spread/depth cost. That is appropriate for the exact LIVE 0/0 universe. It is not the complete Testnet net cost because Testnet taker fees are nonzero. Testnet fee PnL must therefore be reported separately from the zero-fee production counterfactual.

## Correct validation architecture

### Alpha/economic validation — LIVE read-only shadow

Use the actual production markets:

- leader: LIVE Binance USD-M;
- follower: LIVE MEXC;
- pair eligibility: persistent profile + exact LIVE maker=0/taker=0 + Binance cross-list;
- execution simulation: arrival-time LIVE MEXC depth;
- entry latency: one clearly defined latency sample;
- exit latency: a separate clearly defined latency sample;
- both entry and exit use the market book **at their respective simulated arrival times**;
- partial IOC/no top-up semantics are retained;
- exit decisions are sticky but execution starts only after the exit latency expires;
- all timestamps are recorded.

This is the only place where strategy profitability should be judged while LIVE writes remain disabled.

### Execution calibration — MEXC Testnet only

Testnet must measure mechanics, not alpha PnL:

- order-build time;
- POST start -> HTTP response;
- POST start -> terminal order state;
- POST start -> position visible;
- close decision -> POST start;
- close POST -> terminal order state;
- close -> position absent;
- real dealVol, average price, fee, risk limit, leverage and liquidation price.

The calibrator must not substitute Testnet price behavior for LIVE MEXC alpha.

### Production bridge

Only after the LIVE shadow remains profitable under a conservative latency distribution should the same decision engine be connected to an execution adapter. Strategy code and execution code must be separate components, not monkeypatched wrappers.

## Required economic invariant

A signal is not tradable merely because its instantaneous residual is large.

A valid entry must still be economically executable at the **entry-arrival** timestamp:

- signal direction still valid;
- residual retention and leader retention pass;
- partial IOC exists within the allowed limit;
- entry slippage is within limit;
- remaining residual at arrival beats executable cost plus frozen net-edge requirement;
- the selected pair's historical lag persistence is compatible with the execution latency profile.

After an exit decision, PnL is determined only at the **exit-arrival** timestamp, not at the decision timestamp.

## Branch policy

`persistent-end2end-latency-v1` is the clean reconstruction branch.

Reference points remain immutable:

- `known-good-100trade-372c3b2` / `372c3b286eb82aa4b87d806999f8db47173a2b3e` — alpha reference.
- `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5` — frozen parameter/reference point.

The following are historical experiments, not product bases:

- `main` after `8a0bc60`;
- `testnet-known-good-v1`;
- `testnet-frozen-latency-v1`;
- `latency-arb-product-v1`;
- Demo mirror/proxy/direct wrappers.

Do not tune the frozen alpha to hide execution problems. First validate the exact alpha under honest end-to-end latency. If it fails, the result means the known-good paper test was too optimistic for the current transport path; the correct next step is to improve transport or identify longer-lived lag opportunities, not to pretend Testnet PnL validates LIVE alpha.
