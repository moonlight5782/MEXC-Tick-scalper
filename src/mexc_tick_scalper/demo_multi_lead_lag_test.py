from __future__ import annotations

import argparse
import asyncio
import math
import time
import uuid
from dataclasses import dataclass

from . import demo_hybrid_test as hybrid
from .demo_discovery import _fetch_contracts
from .demo_lead_lag_test import _FastLeadLagDemoAdapter, _wait_remote_position
from .demo_live_signal_test import _as_pending_fill
from .demo_smoke import _assert_demo_safety
from .execution import OrderSide, PositionSnapshot
from .lead_lag import LeadLagModel
from .live_lead_lag_scan import MultiBinanceBookTickerFeed
from .live_lead_lag_shadow import BestBook, MexcBestBookFeed, PositiveTrailing
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

LOOP_SECONDS = 0.05
POSITION_WATCH_SECONDS = 0.10
FEE_REFRESH_SECONDS = 5.0
FEE_MAX_AGE_MS = 7_000


@dataclass(frozen=True, slots=True)
class DemoLiveContract:
    live: LiveZeroFeeContract
    demo: dict


@dataclass(frozen=True, slots=True)
class LiveCandidate:
    symbol: str
    direction: int
    edge_bps: float
    required_edge_bps: float
    net_margin_bps: float
    live_spread_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    book: BestBook


def _required_live_edge(*, min_edge_bps: float, live_spread_bps: float, min_net_edge_bps: float, edge_to_spread_ratio: float) -> float:
    return max(
        float(min_edge_bps),
        float(live_spread_bps) + float(min_net_edge_bps),
        float(live_spread_bps) * float(edge_to_spread_ratio),
    )


def _candidate_from_snapshot(symbol: str, snap, book: BestBook, args: argparse.Namespace) -> LiveCandidate | None:
    if not snap.ready:
        return None
    required = _required_live_edge(
        min_edge_bps=args.min_edge_bps,
        live_spread_bps=book.spread_bps,
        min_net_edge_bps=args.min_net_edge_bps,
        edge_to_spread_ratio=args.edge_to_spread_ratio,
    )
    edge = abs(float(snap.edge_bps))
    if edge + 1e-12 < required:
        return None
    return LiveCandidate(
        symbol=symbol,
        direction=int(snap.direction),
        edge_bps=float(snap.edge_bps),
        required_edge_bps=required,
        net_margin_bps=edge - required,
        live_spread_bps=book.spread_bps,
        binance_move_bps=float(snap.binance_move_bps),
        mexc_move_bps=float(snap.mexc_move_bps),
        book=book,
    )


def _best_candidate(candidates: list[LiveCandidate]) -> LiveCandidate | None:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            row.net_margin_bps,
            abs(row.edge_bps),
            -row.live_spread_bps,
        ),
    )


def _zero_fee_status(status) -> bool:
    return (
        status is not None
        and status.maker is not None
        and status.taker is not None
        and float(status.maker) == 0.0
        and float(status.taker) == 0.0
    )


def _signed_move_bps(direction: int, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0 or direction not in (-1, 1):
        return 0.0
    return direction * (current - entry) / entry * 10_000.0


async def _discover_intersection() -> list[DemoLiveContract]:
    live = await discover_live_zero_fee_crosslisted()
    live_by_symbol = {row.mexc_symbol: row for row in live}

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(demo_cfg) as adapter:
        demo_contracts = await _fetch_contracts(adapter)

    out: list[DemoLiveContract] = []
    for row in demo_contracts:
        symbol = str(row.get("symbol") or "").upper()
        live_row = live_by_symbol.get(symbol)
        if live_row is not None:
            out.append(DemoLiveContract(live=live_row, demo=dict(row)))
    out.sort(key=lambda item: item.live.mexc_symbol)
    return out


async def run(args: argparse.Namespace) -> None:
    hybrid._load_project_env()

    intersection = await _discover_intersection()
    if not intersection:
        raise MexcWebError(
            "no exact symbol exists in all three sets: LIVE MEXC account fee=0/0, Binance USD-M, and MEXC Demo. "
            "Demo cannot execute a contract that Testnet does not list."
        )

    symbols = [row.live.mexc_symbol for row in intersection]
    contracts = [row.live for row in intersection]
    by_symbol = {row.live.mexc_symbol: row for row in intersection}

    hybrid.console.print(
        f"[cyan]CONTINUOUS LIVE-LAG -> DEMO[/cyan]: monitoring {len(symbols)} exact-symbol pair(s) continuously; "
        "Binance+MEXC prices are LIVE, all order writes remain TESTNET only."
    )
    hybrid.console.print("Demo-capable LIVE 0/0 symbols: " + ", ".join(symbols))
    hybrid.console.print(
        f"Entry gate: LIVE edge >= max({args.min_edge_bps:.2f}bps, LIVE spread+{args.min_net_edge_bps:.2f}bps, "
        f"LIVE spread*{args.edge_to_spread_ratio:.2f}); fee cache must remain exact 0/0."
    )

    models = {
        symbol: LeadLagModel(
            horizon_ms=args.lead_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            min_edge_bps=args.min_edge_bps,
            min_binance_move_bps=args.min_binance_move_bps,
            max_age_ms=args.max_quote_age_ms,
        )
        for symbol in symbols
    }
    binance = MultiBinanceBookTickerFeed(contracts, models)
    books = MexcBestBookFeed(symbols)
    await binance.start()
    await books.start()

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(demo_cfg)
    live_fee_cfg = WebExecutionConfig.from_env(write_enabled=False)

    position: PositionSnapshot | None = None
    position_symbol = ""
    position_direction = 0
    demo_entry_price = 0.0
    live_entry_price = 0.0
    live_entry_edge_bps = 0.0
    live_entry_spread_bps = 0.0
    live_mfe_bps = 0.0
    live_mae_bps = 0.0
    entry_time = 0.0
    trailing: PositiveTrailing | None = None
    next_position_watch = 0.0
    next_heartbeat = 0.0
    next_fee_refresh = 0.0
    fee_provider = None
    fee_checked_ms = 0
    last_book_ts: dict[str, int] = {symbol: 0 for symbol in symbols}
    last_entry_ms: dict[str, int] = {symbol: -10**18 for symbol in symbols}

    cycles = wins = losses = signals = 0
    live_zero_fee_pnl_usdt = 0.0
    gross_profit = gross_loss = 0.0
    peak_pnl = max_drawdown = 0.0
    deadline = time.monotonic() + float(args.session_seconds)
    warmup_until = time.monotonic() + float(args.warmup_seconds)

    # Precompute TESTNET sizing metadata so no contract-detail lookup is needed on the signal path.
    demo_meta: dict[str, tuple[float, float, int]] = {}

    try:
        async with _FastLeadLagDemoAdapter(demo_cfg) as demo_adapter, MexcWebExecutionAdapter(live_fee_cfg) as live_fee_adapter:
            for symbol in symbols:
                detail = await demo_adapter.get_contract_detail(symbol)
                contract_size = float(detail.get("contractSize") or 0)
                min_vol = float(detail.get("minVol") or 0)
                max_lev = int(detail.get("maxLeverage") or 1)
                if contract_size > 0 and min_vol > 0:
                    demo_meta[symbol] = (contract_size * min_vol, contract_size, max_lev)
            symbols = [symbol for symbol in symbols if symbol in demo_meta]
            if not symbols:
                raise MexcWebError("intersection exists but no Demo contract has valid sizing metadata")

            fee_provider = await read_web_fee_provider(live_fee_adapter)
            fee_checked_ms = int(time.time() * 1000)
            next_fee_refresh = time.monotonic() + FEE_REFRESH_SECONDS

            while time.monotonic() < deadline and cycles < int(args.max_cycles):
                loop_started = time.monotonic()
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                # Feed LIVE MEXC executable mid into the lead-lag model as soon as a new depth update arrives.
                for symbol in symbols:
                    book = books.books.get(symbol)
                    if book is None or book.ts_ms <= last_book_ts[symbol]:
                        continue
                    last_book_ts[symbol] = book.ts_ms
                    models[symbol].update_mexc(bid=book.bid, ask=book.ask, ts_ms=book.ts_ms)

                if now >= next_fee_refresh:
                    fee_provider = await read_web_fee_provider(live_fee_adapter)
                    fee_checked_ms = int(time.time() * 1000)
                    next_fee_refresh = time.monotonic() + FEE_REFRESH_SECONDS

                if position is None:
                    if now < warmup_until or fee_provider is None:
                        if now >= next_heartbeat:
                            hybrid.console.print(
                                f"MULTI HEARTBEAT state=warming symbols={len(symbols)} books={len(books.books)} "
                                f"BinanceQuotes={binance.quotes} fee_age={max(0, now_ms-fee_checked_ms)}ms"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    candidates: list[LiveCandidate] = []
                    for symbol in symbols:
                        book = books.books.get(symbol)
                        if book is None:
                            continue
                        book_age = now_ms - book.ts_ms
                        if book_age < 0 or book_age > float(args.max_book_age_ms):
                            continue
                        if now_ms - last_entry_ms[symbol] < int(args.entry_cooldown_ms):
                            continue
                        fee_status = fee_provider.status(symbol)
                        if not _zero_fee_status(fee_status) or now_ms - fee_checked_ms > FEE_MAX_AGE_MS:
                            continue
                        snap = models[symbol].snapshot(now_ms=now_ms)
                        candidate = _candidate_from_snapshot(symbol, snap, book, args)
                        if candidate is not None:
                            candidates.append(candidate)

                    candidate = _best_candidate(candidates)
                    if candidate is None:
                        if now >= next_heartbeat:
                            ready_count = 0
                            for symbol in symbols:
                                snap = models[symbol].snapshot(now_ms=now_ms)
                                ready_count += int(snap.ready)
                            hybrid.console.print(
                                f"MULTI HEARTBEAT waiting LIVE lag symbols={len(symbols)} books={len(books.books)} "
                                f"raw_ready={ready_count} BinanceQuotes={binance.quotes} fee_age={max(0, now_ms-fee_checked_ms)}ms"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    symbol = candidate.symbol
                    side = OrderSide.LONG if candidate.direction > 0 else OrderSide.SHORT
                    # Final LIVE fee check is from the fresh whole-account cache; never use Demo fees as the strategy gate.
                    fee_status = fee_provider.status(symbol)
                    if not _zero_fee_status(fee_status) or now_ms - fee_checked_ms > FEE_MAX_AGE_MS:
                        continue

                    # Snapshot LIVE entry before the Demo request. Strategy economics and trailing use this LIVE executable price.
                    live_entry = candidate.book.ask if candidate.direction > 0 else candidate.book.bid

                    demo_ask, demo_bid = await asyncio.gather(
                        demo_adapter.get_best_price(symbol, OrderSide.LONG),
                        demo_adapter.get_best_price(symbol, OrderSide.SHORT),
                    )
                    if not (demo_ask > demo_bid > 0):
                        continue
                    demo_mid = (demo_ask + demo_bid) / 2.0
                    demo_spread_bps = (demo_ask - demo_bid) / demo_mid * 10_000.0
                    demo_best = demo_ask if side is OrderSide.LONG else demo_bid
                    min_base_qty, _contract_size, max_lev = demo_meta[symbol]
                    leverage = min(max(1, int(args.leverage)), max_lev)
                    target_notional = max(0.01, float(args.target_margin_usdt)) * leverage
                    requested_qty = max(min_base_qty, target_notional / demo_best)

                    # Reconfirm the LIVE signal after the Demo price lookup. If the lag vanished, do not chase it.
                    fresh_book = books.books.get(symbol)
                    fresh_snap = models[symbol].snapshot(now_ms=int(time.time() * 1000))
                    if fresh_book is None or fresh_book.ts_ms < candidate.book.ts_ms:
                        continue
                    fresh_candidate = _candidate_from_snapshot(symbol, fresh_snap, fresh_book, args)
                    if fresh_candidate is None or fresh_candidate.direction != candidate.direction:
                        continue

                    signals += 1
                    hybrid.console.print(
                        f"LIVE-LAG SIGNAL {symbol} {'LONG' if side is OrderSide.LONG else 'SHORT'} "
                        f"edge={fresh_candidate.edge_bps:+.3f}bps required={fresh_candidate.required_edge_bps:.3f} "
                        f"LIVEspread={fresh_candidate.live_spread_bps:.3f} net_margin={fresh_candidate.net_margin_bps:+.3f} "
                        f"Bmove={fresh_candidate.binance_move_bps:+.3f} Mmove={fresh_candidate.mexc_move_bps:+.3f} "
                        f"DemoSpread={demo_spread_bps:.3f}"
                    )

                    fill = await demo_adapter.open_ioc(
                        symbol=symbol,
                        side=side,
                        price=demo_best,
                        qty=requested_qty,
                        leverage=leverage,
                        client_order_id=f"live-lag-demo-{uuid.uuid4().hex}",
                    )
                    remote = await demo_adapter.get_position(symbol)
                    if remote is None and (fill.order_id or fill.position_id or fill.filled_qty > 0):
                        remote = await _wait_remote_position(demo_adapter, symbol, side)
                    if remote is None:
                        continue
                    if remote.side is not side:
                        raise MexcWebError("Demo remote position side mismatch")

                    position = remote
                    position_symbol = symbol
                    position_direction = candidate.direction
                    demo_entry_price = remote.entry_price or fill.avg_price or demo_best
                    live_entry_price = live_entry
                    live_entry_edge_bps = fresh_candidate.edge_bps
                    live_entry_spread_bps = fresh_candidate.live_spread_bps
                    live_mfe_bps = live_mae_bps = 0.0
                    entry_time = time.monotonic()
                    trailing = PositiveTrailing(distance_bps=max(args.trailing_distance_bps, live_entry_spread_bps))
                    next_position_watch = 0.0
                    last_entry_ms[symbol] = int(time.time() * 1000)
                    hybrid.console.print(
                        f"DEMO ENTRY {symbol} {'LONG' if side is OrderSide.LONG else 'SHORT'} qty={remote.qty:g} "
                        f"DemoEntry={demo_entry_price:g} LIVEEntry={live_entry_price:g} LIVEedge={live_entry_edge_bps:+.3f} "
                        f"LIVEspread={live_entry_spread_bps:.3f} DemoFeeReported={fill.fee_usdt:g}"
                    )
                    continue

                assert position is not None and position_symbol and position_direction in (-1, 1)
                assert trailing is not None

                if now < next_position_watch:
                    await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                    continue
                next_position_watch = now + POSITION_WATCH_SECONDS

                book = books.books.get(position_symbol)
                if book is None:
                    await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                    continue
                live_exit_price = book.bid if position_direction > 0 else book.ask
                live_move_bps = _signed_move_bps(position_direction, live_entry_price, live_exit_price)
                live_mfe_bps = max(live_mfe_bps, live_move_bps)
                live_mae_bps = min(live_mae_bps, live_move_bps)
                trail = trailing.update(live_move_bps)
                age_s = now - entry_time
                snap = models[position_symbol].snapshot(now_ms=int(time.time() * 1000))

                reason: str | None = None
                if trail is not None and live_move_bps <= trail and age_s >= args.min_hold_seconds:
                    reason = "positive_trailing_stop"
                adverse_limit = max(args.adverse_cut_bps, live_entry_spread_bps * args.adverse_spread_mult)
                if reason is None and live_move_bps <= -adverse_limit and age_s >= args.min_hold_seconds:
                    reason = "live_adverse_cut"
                convergence = max(args.convergence_bps, abs(live_entry_edge_bps) * args.convergence_fraction)
                if reason is None and snap.age_ms <= args.max_quote_age_ms and abs(snap.edge_bps) <= convergence:
                    reason = "lead_lag_converged"
                if (
                    reason is None
                    and snap.age_ms <= args.max_quote_age_ms
                    and snap.direction == -position_direction
                    and abs(snap.edge_bps) >= args.reversal_edge_bps
                ):
                    reason = "lead_lag_reversed"
                if (
                    reason is None
                    and snap.age_ms <= args.max_quote_age_ms
                    and snap.binance_move_bps * position_direction <= -args.min_binance_move_bps
                ):
                    reason = "binance_reversal"
                if reason is None and age_s >= args.max_hold_seconds:
                    reason = "lead_lag_timeout"

                if now >= next_heartbeat:
                    trail_txt = "OFF" if trail is None else f"+{trail:.3f}bps"
                    hybrid.console.print(
                        f"DEMO POSITION {position_symbol} LIVEmark={live_move_bps:+.3f}bps "
                        f"MFE={live_mfe_bps:+.3f} MAE={live_mae_bps:+.3f} TRAIL={trail_txt} "
                        f"LIVEedge={snap.edge_bps:+.3f} Bmove={snap.binance_move_bps:+.3f}"
                    )
                    next_heartbeat = now + float(args.heartbeat_seconds)

                if reason is None:
                    await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                    continue

                demo_fill = await hybrid._flatten_position(demo_adapter, position, reason)
                live_pnl_usdt = float(args.target_margin_usdt) * min(int(args.leverage), demo_meta[position_symbol][2]) * live_move_bps / 10_000.0
                live_zero_fee_pnl_usdt += live_pnl_usdt
                peak_pnl = max(peak_pnl, live_zero_fee_pnl_usdt)
                max_drawdown = max(max_drawdown, peak_pnl - live_zero_fee_pnl_usdt)
                if live_pnl_usdt > 0:
                    wins += 1
                    gross_profit += live_pnl_usdt
                elif live_pnl_usdt < 0:
                    losses += 1
                    gross_loss += abs(live_pnl_usdt)
                cycles += 1
                hybrid.console.print(
                    f"DEMO EXIT {position_symbol} reason={reason} LIVEexit={live_exit_price:g} "
                    f"LIVEpnl={live_pnl_usdt:+.6f}USDT ({live_move_bps:+.3f}bps) "
                    f"MFE={live_mfe_bps:+.3f} MAE={live_mae_bps:+.3f} "
                    f"DemoExit={demo_fill.avg_price:g} DemoFeeReported={demo_fill.fee_usdt:g}"
                )

                position = None
                position_symbol = ""
                position_direction = 0
                demo_entry_price = live_entry_price = live_entry_edge_bps = live_entry_spread_bps = 0.0
                live_mfe_bps = live_mae_bps = 0.0
                entry_time = 0.0
                trailing = None

            if position is not None:
                book = books.books.get(position_symbol)
                live_exit_price = (book.bid if position_direction > 0 else book.ask) if book else live_entry_price
                live_move_bps = _signed_move_bps(position_direction, live_entry_price, live_exit_price)
                demo_fill = await hybrid._flatten_position(demo_adapter, position, "session_timeout")
                live_pnl_usdt = float(args.target_margin_usdt) * min(int(args.leverage), demo_meta[position_symbol][2]) * live_move_bps / 10_000.0
                live_zero_fee_pnl_usdt += live_pnl_usdt
                if live_pnl_usdt > 0:
                    wins += 1
                    gross_profit += live_pnl_usdt
                elif live_pnl_usdt < 0:
                    losses += 1
                    gross_loss += abs(live_pnl_usdt)
                cycles += 1
                hybrid.console.print(
                    f"SESSION FLATTEN {position_symbol} LIVEpnl={live_pnl_usdt:+.6f}USDT DemoExit={demo_fill.avg_price:g}"
                )
    finally:
        await binance.close()
        await books.close()

    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = wins / max(1, wins + losses) * 100.0
    hybrid.console.print(
        f"MULTI LIVE-LAG COMPLETE trades={cycles} signals={signals} wins={wins} losses={losses} "
        f"win_rate={win_rate:.1f}% PF={pf:.2f} ZERO_FEE_LIVE_PNL={live_zero_fee_pnl_usdt:+.6f}USDT "
        f"max_drawdown={max_drawdown:.6f}USDT"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuous multi-pair LIVE Binance->MEXC lag with TESTNET execution")
    parser.add_argument("--session-seconds", type=float, default=1800.0)
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--target-margin-usdt", type=float, default=2.0)
    parser.add_argument("--warmup-seconds", type=float, default=8.0)
    parser.add_argument("--lead-horizon-ms", type=int, default=250)
    parser.add_argument("--baseline-seconds", type=float, default=20.0)
    parser.add_argument("--min-edge-bps", type=float, default=4.0)
    parser.add_argument("--min-net-edge-bps", type=float, default=2.0)
    parser.add_argument("--edge-to-spread-ratio", type=float, default=1.15)
    parser.add_argument("--min-binance-move-bps", type=float, default=1.0)
    parser.add_argument("--max-quote-age-ms", type=float, default=500.0)
    parser.add_argument("--max-book-age-ms", type=float, default=750.0)
    parser.add_argument("--entry-cooldown-ms", type=int, default=750)
    parser.add_argument("--heartbeat-seconds", type=float, default=2.0)
    parser.add_argument("--min-hold-seconds", type=float, default=0.15)
    parser.add_argument("--max-hold-seconds", type=float, default=60.0)
    parser.add_argument("--adverse-cut-bps", type=float, default=4.0)
    parser.add_argument("--adverse-spread-mult", type=float, default=1.25)
    parser.add_argument("--convergence-bps", type=float, default=1.0)
    parser.add_argument("--convergence-fraction", type=float, default=0.25)
    parser.add_argument("--reversal-edge-bps", type=float, default=2.0)
    parser.add_argument("--trailing-distance-bps", type=float, default=1.5)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        hybrid.console.print(f"[red]MULTI LIVE-LAG DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
