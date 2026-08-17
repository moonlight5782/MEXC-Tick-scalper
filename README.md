# MEXC Tick Scalper

Current development stage: **known-good lead/lag strategy → real MEXC Testnet execution validation**.

## Source of truth

Read [`PROJECT_HANDOFF.md`](PROJECT_HANDOFF.md) before changing strategy or execution code. It contains the complete architecture, frozen invariants, Testnet risk contract, known limitations, acceptance criteria, and handoff instructions.

## Known-good strategy

Immutable reference branch:

```text
known-good-100trade-372c3b2
372c3b286eb82aa4b87d806999f8db47173a2b3e
```

This is the code state that completed the validated LIVE-data paper run of 100 closed trades. Do not modify that branch.

## Active Testnet branch

```text
testnet-known-good-v1
```

The active Testnet runner preserves the known-good signal/entry/exit semantics and replaces virtual execution with real same-symbol MEXC Testnet IOC execution plus liquidation/emergency safeguards.

### Supported launchers

Known-good paper reference:

```powershell
.\start_live_shadow.bat
```

Real Testnet validation:

```powershell
.\start_testnet_known_good_v1.bat
```

The Testnet launcher uses LIVE Binance + LIVE MEXC public market data for signals and sends order writes only to `futures.testnet.mexc.com`.

## Critical IOC rule

Each entry makes one IOC request for the target notional. If only part can fill inside the IOC limit, only that actual fill is managed; the remainder is cancelled. There is no top-up/chase/retry to reach the requested notional.

Example: request $10,000, actual IOC fill ~$2,000 → manage only ~$2,000.

## Safety

- Never commit or share `MEXC_DEMO_WEB_TOKEN`.
- No LIVE order writes are allowed during this stage.
- Do not use proxy symbols.
- Do not enable exchange-maximum leverage automatically.
- Do not change alpha thresholds to solve exchange/Testnet execution bugs.
- A Testnet trade is valid only when the remote Testnet order/position confirms an actual fill.

This software is experimental. The known-good paper result is not a guarantee of future or Testnet profitability.
