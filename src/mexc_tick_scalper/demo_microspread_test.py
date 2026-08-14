from __future__ import annotations

import argparse
import asyncio
import csv
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import demo_hybrid_test as hybrid
from .demo_discovery import _fetch_contracts
from .demo_lead_lag_test import _FastLeadLagDemoAdapter, _wait_remote_position
from .demo_smoke import _assert_demo_safety
from .execution import OrderSide, PositionSnapshot
from .live_lead_lag_shadow import PositiveTrailing
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel, MicroSpreadSnapshot
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

FEE_REFRESH_SECONDS = 5.0
FEE_MAX_AGE_MS = 7_000


@dataclass(frozen=True, slots=True)
class DemoLiveContract:
    live: LiveZeroFeeContract
    demo: dict


@dataclass(frozen=True, slots=True)
class MicroCandidate:
    symbol: str
    direction: int
    edge_bps: float
    threshold_bps: float
    net_margin_bps: float
    spread_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    book: LiveBook
    snapshot: MicroSpreadSnapshot


def _zero_fee_status(status) -> bool:
    return (
        status is not None
        and status.maker is not None
        and status.taker is not None
        and float(status.maker) == 0.0
        and float(status.taker) == 0.0
    )


def _required_edge(*, spread_bps: float, min_edge_bps: float, min_net_edge_bps: float, spread_ratio: float) -> float:
    return max(
        float(min_edge_bps),
        float(spread_bps) + float(min_net_edge_bps),
        float(spread_bps) * float(spread_ratio),
    )


def _signed_move_bps(direction: int, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0 or direction not in (-1, 1):
        return 0.0
    return direction * (current - entry) / entry * 10_000.0


def _candidate_from_model(
    symbol: str,
    model: MicroSpreadModel,
    book: LiveBook,
    args: argparse.Namespace,
    *,
    now_ms: int,
    consume: bool = False,
) -> MicroCandidate | None:
    threshold = _required_edge(
        spread_bps=book.spread_bps,
        min_edge_bps=args.min_edge_bps,
        min_net_edge_bps=args.min_net_edge_bps,
        spread_ratio=args.edge_to_spread_ratio,
    )
    snap = model.signal(now_ms=now_ms, threshold_bps=threshold) if consume else model.snapshot(
        now_ms=now_ms,
        threshold_bps=threshold,
    )
    if not snap.ready:
        return None
    edge = abs(float(snap.edge_bps))
    return MicroCandidate(
        symbol=symbol,
        direction=int(snap.direction),
        edge_bps=float(snap.edge_bps),
        threshold_bps=float(threshold),
        net_margin_bps=edge - float(threshold),
        spread_bps=book.spread_bps,
        binance_move_bps=float(snap.binance_move_bps),
        mexc_move_bps=float(snap.mexc_move_bps),
        book=book,
        snapshot=snap,
    )


def _best_candidate(rows: list[MicroCandidate]) -> MicroCandidate | None:
    if not rows:
        return None
    return max(rows, key=lambda row: (row.net_margin_bps, abs(row.edge_bps), -row.spread_bps))


EXCURSION_FIELDS = (
    "timestamp_ms", "symbol", "direction", "residual_bps", "threshold_bps",
    "net_margin_bps", "spread_bps", "binance_move_bps", "mexc_move_bps",
    "binance_age_ms", "mexc_age_ms",
)


def _append_excursion(path: Path, candidate: MicroCandidate, *, timestamp_ms: int) -> None:
    """Append one structured row for each hysteresis-consumed LIVE excursion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXCURSION_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow({
            "timestamp_ms": int(timestamp_ms),
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "residual_bps": f"{candidate.edge_bps:.9f}",
            "threshold_bps": f"{candidate.threshold_bps:.9f}",
            "net_margin_bps": f"{candidate.net_margin_bps:.9f}",
            "spread_bps": f"{candidate.spread_bps:.9f}",
            "binance_move_bps": f"{candidate.binance_move_bps:.9f}",
            "mexc_move_bps": f"{candidate.mexc_move_bps:.9f}",
            "binance_age_ms": f"{candidate.snapshot.binance_age_ms:.3f}",
            "mexc_age_ms": f"{candidate.snapshot.mexc_age_ms:.3f}",
        })


async def _discover_intersection() -> list[DemoLiveContract]:
    live_rows = await discover_live_zero_fee_crosslisted()
    live_by_symbol = {row.mexc_symbol: row for row in live_rows}

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(demo_cfg) as adapter:
        demo_rows = await _fetch_contracts(adapter)

    out: list[DemoLiveContract] = []
    for row in demo_rows:
        symbol = str(row.get("symbol") or "").upper()
        live = live_by_symbol.get(symbol)
        if live is not None:
            out.append(DemoLiveContract(live=live, demo=dict(row)))
    out.sort(key=lambda row: row.live.mexc_symbol)
    return out


async def run(args: argparse.Namespace) -> None:
    hybrid._load_project_env()
    intersection = await _discover_intersection()
    if not intersection:
        raise MexcWebError(
            "no exact symbol exists in LIVE MEXC fee=0/0, Binance USD-M and MEXC Demo simultaneously"
        )

    excursion_csv = Path(args.excursion_csv) if args.excursion_csv else Path(
        f"microspread_excursions_{int(time.time())}.csv"
    )

    contracts = [row.live for row in intersection]
    symbols = [row.live.mexc_symbol for row in intersection]
    wake = asyncio.Event()
    models = {
        symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=args.min_edge_bps,
            min_binance_move_bps=args.min_binance_move_bps,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
            rearm_fraction=args.rearm_fraction,
        )
        for symbol in symbols
    }

    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake)
    await binance.start()
    await mexc.start()

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(demo_cfg)
    live_fee_cfg = WebExecutionConfig.from_env(write_enabled=False)

    hybrid.console.print(
        f"[cyan]LIVE MICROSPREAD -> DEMO[/cyan]: {len(symbols)} symbol(s); event-driven Binance bookTicker + "
        "MEXC depth; all order writes TESTNET only."
    )
    hybrid.console.print(f"Excursion telemetry CSV: {excursion_csv.resolve()}")
    hybrid.console.print("Symbols: " + ", ".join(symbols))
    hybrid.console.print(
        f"Micro gate: residual >= max({args.min_edge_bps:.2f}bps, LIVE spread+{args.min_net_edge_bps:.2f}bps, "
        f"spread*{args.edge_to_spread_ratio:.2f}); Binance micro-move >= {args.min_binance_move_bps:.3f}bps; "
        f"basis baseline={args.baseline_seconds:g}s excluding newest {args.baseline_exclusion_ms}ms."
    )

    demo_meta: dict[str, tuple[float, int]] = {}
    fee_provider = None
    fee_checked_ms = 0
    next_fee_refresh = 0.0
    next_heartbeat = 0.0
    warmup_until = time.monotonic() + float(args.warmup_seconds)
    deadline = time.monotonic() + float(args.session_seconds)
    last_entry_ms = {symbol: -10**18 for symbol in symbols}

    position: PositionSnapshot | None = None
    position_symbol = ""
    position_direction = 0
    live_entry_price = 0.0
    live_entry_edge_bps = 0.0
    live_entry_spread_bps = 0.0
    entry_time = 0.0
    live_mfe_bps = 0.0
    live_mae_bps = 0.0
    trailing: PositiveTrailing | None = None

    cycles = signals = wins = losses = 0
    total_live_pnl = 0.0
    gross_profit = gross_loss = 0.0
    peak_pnl = max_drawdown = 0.0
    excursions_seen = 0
    last_excursion_key: tuple[str, int] | None = None

    try:
        async with _FastLeadLagDemoAdapter(demo_cfg) as demo_adapter, MexcWebExecutionAdapter(live_fee_cfg) as live_fee_adapter:
            for symbol in symbols:
                detail = await demo_adapter.get_contract_detail(symbol)
                contract_size = float(detail.get("contractSize") or 0)
                min_vol = float(detail.get("minVol") or 0)
                max_lev = int(detail.get("maxLeverage") or 1)
                if contract_size > 0 and min_vol > 0:
                    demo_meta[symbol] = (contract_size * min_vol, max_lev)
            symbols = [symbol for symbol in symbols if symbol in demo_meta]
            if not symbols:
                raise MexcWebError("no Demo contract has valid sizing metadata")

            fee_provider = await read_web_fee_provider(live_fee_adapter)
            fee_checked_ms = int(time.time() * 1000)
            next_fee_refresh = time.monotonic() + FEE_REFRESH_SECONDS

            while time.monotonic() < deadline and cycles < int(args.max_cycles):
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                if now >= next_fee_refresh:
                    fee_provider = await read_web_fee_provider(live_fee_adapter)
                    fee_checked_ms = int(time.time() * 1000)
                    next_fee_refresh = time.monotonic() + FEE_REFRESH_SECONDS

                if position is None:
                    candidates: list[MicroCandidate] = []
                    max_residual = 0.0
                    above_floor = 0
                    if now >= warmup_until and fee_provider is not None:
                        for symbol in symbols:
                            book = mexc.books.get(symbol)
                            if book is None:
                                continue
                            book_age = now_ms - book.recv_ms
                            if book_age < 0 or book_age > float(args.max_book_age_ms):
                                continue
                            status = fee_provider.status(symbol)
                            if not _zero_fee_status(status) or now_ms - fee_checked_ms > FEE_MAX_AGE_MS:
                                continue
                            if now_ms - last_entry_ms[symbol] < int(args.entry_cooldown_ms):
                                continue

                            raw = models[symbol].snapshot(now_ms=now_ms, threshold_bps=args.min_edge_bps)
                            max_residual = max(max_residual, abs(raw.edge_bps))
                            above_floor += int(abs(raw.edge_bps) >= args.min_edge_bps)

                            candidate = _candidate_from_model(symbol, models[symbol], book, args, now_ms=now_ms)
                            if candidate is not None:
                                candidates.append(candidate)

                    best = _best_candidate(candidates)
                    if best is not None:
                        # Consume the hysteresis crossing only for the winner.
                        consumed = _candidate_from_model(
                            best.symbol, models[best.symbol], best.book, args, now_ms=now_ms, consume=True
                        )
                        if consumed is not None:
                            _append_excursion(excursion_csv, consumed, timestamp_ms=now_ms)
                            excursion_key = (consumed.symbol, consumed.direction)
                            if excursion_key != last_excursion_key:
                                excursions_seen += 1
                                last_excursion_key = excursion_key

                            side = OrderSide.LONG if consumed.direction > 0 else OrderSide.SHORT
                            status = fee_provider.status(consumed.symbol)
                            if _zero_fee_status(status) and now_ms - fee_checked_ms <= FEE_MAX_AGE_MS:
                                # Capture the LIVE executable entry before any Demo REST call.
                                live_entry = consumed.book.ask if consumed.direction > 0 else consumed.book.bid

                                demo_ask, demo_bid = await asyncio.gather(
                                    demo_adapter.get_best_price(consumed.symbol, OrderSide.LONG),
                                    demo_adapter.get_best_price(consumed.symbol, OrderSide.SHORT),
                                )
                                if demo_ask > demo_bid > 0:
                                    # Never chase an excursion that vanished while Testnet price was fetched.
                                    fresh_now_ms = int(time.time() * 1000)
                                    fresh_book = mexc.books.get(consumed.symbol)
                                    fresh = None
                                    if fresh_book is not None:
                                        fresh = _candidate_from_model(
                                            consumed.symbol,
                                            models[consumed.symbol],
                                            fresh_book,
                                            args,
                                            now_ms=fresh_now_ms,
                                            consume=False,
                                        )
                                    if fresh is not None and fresh.direction == consumed.direction:
                                        demo_best = demo_ask if side is OrderSide.LONG else demo_bid
                                        min_base_qty, max_lev = demo_meta[consumed.symbol]
                                        leverage = min(max(1, int(args.leverage)), max_lev)
                                        notional = max(0.01, float(args.target_margin_usdt)) * leverage
                                        requested_qty = max(min_base_qty, notional / demo_best)
                                        signals += 1
                                        hybrid.console.print(
                                            f"MICRO SIGNAL {consumed.symbol} {'LONG' if consumed.direction > 0 else 'SHORT'} "
                                            f"residual={fresh.edge_bps:+.3f}bps threshold={fresh.threshold_bps:.3f} "
                                            f"spread={fresh.spread_bps:.3f} net={fresh.net_margin_bps:+.3f} "
                                            f"B100={fresh.binance_move_bps:+.3f} M100={fresh.mexc_move_bps:+.3f} "
                                            f"Bage={fresh.snapshot.binance_age_ms:.0f}ms Mage={fresh.snapshot.mexc_age_ms:.0f}ms"
                                        )
                                        fill = await demo_adapter.open_ioc(
                                            symbol=consumed.symbol,
                                            side=side,
                                            price=demo_best,
                                            qty=requested_qty,
                                            leverage=leverage,
                                            client_order_id=f"microspread-demo-{uuid.uuid4().hex}",
                                        )
                                        remote = await demo_adapter.get_position(consumed.symbol)
                                        if remote is None and (fill.order_id or fill.position_id or fill.filled_qty > 0):
                                            remote = await _wait_remote_position(demo_adapter, consumed.symbol, side)
                                        if remote is not None:
                                            if remote.side is not side:
                                                raise MexcWebError("Demo microspread position side mismatch")
                                            position = remote
                                            position_symbol = consumed.symbol
                                            position_direction = consumed.direction
                                            live_entry_price = live_entry
                                            live_entry_edge_bps = fresh.edge_bps
                                            live_entry_spread_bps = fresh.spread_bps
                                            entry_time = time.monotonic()
                                            live_mfe_bps = live_mae_bps = 0.0
                                            trailing = PositiveTrailing(
                                                distance_bps=max(args.trailing_distance_bps, live_entry_spread_bps)
                                            )
                                            last_entry_ms[position_symbol] = int(time.time() * 1000)
                                            hybrid.console.print(
                                                f"DEMO ENTRY {position_symbol} {'LONG' if position_direction > 0 else 'SHORT'} "
                                                f"qty={remote.qty:g} LIVEEntry={live_entry_price:g} residual={live_entry_edge_bps:+.3f} "
                                                f"spread={live_entry_spread_bps:.3f} DemoFeeReported={fill.fee_usdt:g}"
                                            )

                    if position is None and now >= next_heartbeat:
                        phase = "warming" if now < warmup_until else "watching"
                        hybrid.console.print(
                            f"MICRO HEARTBEAT state={phase} symbols={len(symbols)} books={len(mexc.books)} "
                            f"Bquotes={binance.quotes} Mdepth={mexc.updates} above_floor={above_floor} "
                            f"max_residual={max_residual:.3f}bps candidates={len(candidates)} "
                            f"fee_age={max(0, now_ms-fee_checked_ms)}ms"
                        )
                        next_heartbeat = now + float(args.heartbeat_seconds)

                else:
                    assert position_symbol and position_direction in (-1, 1) and trailing is not None
                    book = mexc.books.get(position_symbol)
                    if book is not None:
                        live_exit = book.bid if position_direction > 0 else book.ask
                        move_bps = _signed_move_bps(position_direction, live_entry_price, live_exit)
                        live_mfe_bps = max(live_mfe_bps, move_bps)
                        live_mae_bps = min(live_mae_bps, move_bps)
                        trail = trailing.update(move_bps)
                        age_s = now - entry_time
                        snap = models[position_symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)

                        reason: str | None = None
                        if trail is not None and move_bps <= trail and age_s >= args.min_hold_seconds:
                            reason = "positive_trailing_stop"
                        adverse = max(args.adverse_cut_bps, live_entry_spread_bps * args.adverse_spread_mult)
                        if reason is None and move_bps <= -adverse and age_s >= args.min_hold_seconds:
                            reason = "live_adverse_cut"
                        convergence = max(args.convergence_bps, abs(live_entry_edge_bps) * args.convergence_fraction)
                        if reason is None and abs(snap.edge_bps) <= convergence:
                            reason = "microspread_converged"
                        if (
                            reason is None
                            and snap.direction == -position_direction
                            and abs(snap.edge_bps) >= args.reversal_edge_bps
                        ):
                            reason = "microspread_reversed"
                        if (
                            reason is None
                            and snap.binance_move_bps * position_direction <= -args.min_binance_move_bps
                            and age_s >= args.min_hold_seconds
                        ):
                            reason = "binance_micro_reversal"
                        if reason is None and age_s >= args.max_hold_seconds:
                            reason = "microspread_timeout"

                        if now >= next_heartbeat:
                            trail_txt = "OFF" if trail is None else f"+{trail:.3f}bps"
                            hybrid.console.print(
                                f"MICRO POSITION {position_symbol} mark={move_bps:+.3f}bps MFE={live_mfe_bps:+.3f} "
                                f"MAE={live_mae_bps:+.3f} TRAIL={trail_txt} residual={snap.edge_bps:+.3f}bps "
                                f"B100={snap.binance_move_bps:+.3f}"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)

                        if reason is not None:
                            demo_fill = await hybrid._flatten_position(demo_adapter, position, reason)
                            leverage = min(int(args.leverage), demo_meta[position_symbol][1])
                            live_pnl = float(args.target_margin_usdt) * leverage * move_bps / 10_000.0
                            total_live_pnl += live_pnl
                            peak_pnl = max(peak_pnl, total_live_pnl)
                            max_drawdown = max(max_drawdown, peak_pnl - total_live_pnl)
                            if live_pnl > 0:
                                wins += 1
                                gross_profit += live_pnl
                            elif live_pnl < 0:
                                losses += 1
                                gross_loss += abs(live_pnl)
                            cycles += 1
                            hybrid.console.print(
                                f"MICRO EXIT {position_symbol} reason={reason} LIVEpnl={live_pnl:+.6f}USDT "
                                f"move={move_bps:+.3f}bps MFE={live_mfe_bps:+.3f} MAE={live_mae_bps:+.3f} "
                                f"DemoExit={demo_fill.avg_price:g}"
                            )
                            position = None
                            position_symbol = ""
                            position_direction = 0
                            live_entry_price = live_entry_edge_bps = live_entry_spread_bps = 0.0
                            entry_time = live_mfe_bps = live_mae_bps = 0.0
                            trailing = None

                if total_live_pnl <= -abs(float(args.max_session_loss_usdt)):
                    hybrid.console.print(
                        f"[yellow]MICRO RISK HALT[/yellow]: modeled zero-fee LIVE PnL {total_live_pnl:+.6f}USDT "
                        f"<= -{abs(float(args.max_session_loss_usdt)):.6f}USDT"
                    )
                    break

                # Event-driven: sleep only until the next market-data update or a
                # short maintenance timeout for fees/heartbeats.
                if not wake.is_set():
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=float(args.idle_timeout_seconds))
                    except TimeoutError:
                        pass
                wake.clear()

            if position is not None:
                book = mexc.books.get(position_symbol)
                live_exit = (book.bid if position_direction > 0 else book.ask) if book else live_entry_price
                move_bps = _signed_move_bps(position_direction, live_entry_price, live_exit)
                demo_fill = await hybrid._flatten_position(demo_adapter, position, "session_end")
                leverage = min(int(args.leverage), demo_meta[position_symbol][1])
                live_pnl = float(args.target_margin_usdt) * leverage * move_bps / 10_000.0
                total_live_pnl += live_pnl
                cycles += 1
                hybrid.console.print(
                    f"SESSION FLATTEN {position_symbol} LIVEpnl={live_pnl:+.6f}USDT DemoExit={demo_fill.avg_price:g}"
                )
    finally:
        await binance.close()
        await mexc.close()

    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = wins / max(1, wins + losses) * 100.0
    hybrid.console.print(
        f"MICROSPREAD COMPLETE trades={cycles} signals={signals} excursions={excursions_seen} wins={wins} losses={losses} "
        f"win_rate={win_rate:.1f}% PF={pf:.2f} ZERO_FEE_LIVE_MODEL_PNL={total_live_pnl:+.6f}USDT "
        f"max_drawdown={max_drawdown:.6f}USDT"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Event-driven LIVE Binance/MEXC microspread with TESTNET execution")
    parser.add_argument("--session-seconds", type=float, default=1800.0)
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--target-margin-usdt", type=float, default=2.0)
    parser.add_argument("--warmup-seconds", type=float, default=3.0)
    parser.add_argument("--micro-horizon-ms", type=int, default=100)
    parser.add_argument("--baseline-seconds", type=float, default=8.0)
    parser.add_argument("--baseline-exclusion-ms", type=int, default=1000)
    parser.add_argument("--min-edge-bps", type=float, default=0.35)
    parser.add_argument("--min-net-edge-bps", type=float, default=0.20)
    parser.add_argument("--edge-to-spread-ratio", type=float, default=1.05)
    parser.add_argument("--min-binance-move-bps", type=float, default=0.02)
    parser.add_argument("--max-binance-age-ms", type=float, default=300.0)
    parser.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    parser.add_argument("--max-book-age-ms", type=float, default=2000.0)
    parser.add_argument("--rearm-fraction", type=float, default=0.35)
    parser.add_argument("--entry-cooldown-ms", type=int, default=250)
    parser.add_argument("--heartbeat-seconds", type=float, default=2.0)
    parser.add_argument("--idle-timeout-seconds", type=float, default=0.05)
    parser.add_argument("--min-hold-seconds", type=float, default=0.05)
    parser.add_argument("--max-hold-seconds", type=float, default=15.0)
    parser.add_argument("--adverse-cut-bps", type=float, default=1.5)
    parser.add_argument("--adverse-spread-mult", type=float, default=1.25)
    parser.add_argument("--convergence-bps", type=float, default=0.10)
    parser.add_argument("--convergence-fraction", type=float, default=0.20)
    parser.add_argument("--reversal-edge-bps", type=float, default=0.20)
    parser.add_argument("--trailing-distance-bps", type=float, default=1.0)
    parser.add_argument("--max-session-loss-usdt", type=float, default=0.50)
    parser.add_argument("--excursion-csv", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        hybrid.console.print(f"[red]MICROSPREAD DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
