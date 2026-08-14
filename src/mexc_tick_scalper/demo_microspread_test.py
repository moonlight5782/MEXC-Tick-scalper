from __future__ import annotations

import argparse
import asyncio
import csv
import math
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from . import demo_hybrid_test as hybrid
from .demo_discovery import _fetch_contracts
from .demo_lead_lag_test import _FastLeadLagDemoAdapter
from .demo_position_manager import flatten_all_demo_positions, wait_account_flat
from .demo_smoke import _assert_demo_safety
from .execution import OrderFill, OrderSide, PositionSnapshot
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .live_lead_lag_shadow import PositiveTrailing
from .live_zero_fee_universe import (
    LIVE_REST,
    LIVE_WS,
    LiveZeroFeeContract,
    discover_live_zero_fee_crosslisted,
)
from .market import MexcPublicMarket
from .microspread import MicroSpreadModel, MicroSpreadSnapshot
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

FEE_REFRESH_SECONDS = 5.0
FEE_MAX_AGE_MS = 7_000
DEMO_WS = "wss://futures.testnet.mexc.com/edge"
AMBIGUOUS_IOC_FLAT_TIMEOUT_SECONDS = 12.0
EXACT_CLOSE_VISIBILITY_SECONDS = 5.0
MAX_VOLUME_DEMO_QUARANTINE = frozenset({"ARB_USDT", "RAVE_USDT", "SEI_USDT"})
DEMO_ROUND_TRIP_FEE_RATE = 0.0004
DEMO_BALANCE_SAFETY_FRACTION = 0.98


def _estimated_demo_net_bps(
    *,
    direction: int,
    entry_price: float,
    exit_price: float,
    qty: float,
    entry_fee_usdt: float,
    exit_fee_rate: float,
) -> tuple[float, float, float]:
    """Return executable Demo gross/net PnL and net return in basis points."""
    gross = direction * (exit_price - entry_price) * qty
    exit_fee = max(0.0, exit_fee_rate) * exit_price * qty
    net = gross - max(0.0, entry_fee_usdt) - exit_fee
    notional = entry_price * qty
    net_bps = net / notional * 10_000.0 if notional > 0 else -math.inf
    return gross, net, net_bps


def _convergence_exit_allowed(
    *,
    current_edge_bps: float,
    entry_edge_bps: float,
    convergence_bps: float,
    convergence_fraction: float,
    demo_net_bps: float,
    min_exit_profit_bps: float,
) -> bool:
    convergence = max(
        max(0.0, convergence_bps),
        abs(entry_edge_bps) * max(0.0, convergence_fraction),
    )
    return abs(current_edge_bps) <= convergence and demo_net_bps >= min_exit_profit_bps


def _profitable_reversal_exit_allowed(
    *,
    current_direction: int,
    position_direction: int,
    current_edge_bps: float,
    entry_threshold_bps: float,
    reversal_edge_bps: float,
    demo_net_bps: float,
    min_exit_profit_bps: float,
) -> bool:
    meaningful_reversal = max(max(0.0, reversal_edge_bps), max(0.0, entry_threshold_bps))
    return (
        current_direction == -position_direction
        and abs(current_edge_bps) >= meaningful_reversal
        and demo_net_bps >= min_exit_profit_bps
    )


def _confirmed_candidate(
    pending: tuple[str, int, int] | None,
    candidate: MicroCandidate | None,
    *,
    now_ms: int,
    confirm_ms: int,
) -> tuple[bool, tuple[str, int, int] | None]:
    """Require the same symbol/direction to remain executable for a minimum time."""
    if candidate is None:
        return False, None
    required_ms = max(0, int(confirm_ms))
    if required_ms == 0:
        return True, (candidate.symbol, candidate.direction, now_ms)
    if pending is None or pending[:2] != (candidate.symbol, candidate.direction):
        return False, (candidate.symbol, candidate.direction, now_ms)
    return now_ms - pending[2] >= required_ms, pending


def _cycle_margin_usdt(
    *, fixed_margin_usdt: float, strategy_equity_usdt: float,
    target_exposure_multiple: float, leverage: int, target_notional_usdt: float = 0.0,
) -> float:
    if target_notional_usdt > 0:
        return max(0.01, target_notional_usdt / max(1, leverage))
    if target_exposure_multiple <= 0:
        return max(0.01, fixed_margin_usdt)
    return max(0.01, strategy_equity_usdt * target_exposure_multiple / max(1, leverage))


def _dynamic_sizing_ready(
    *, completed_trades: int, profit_usdt: float, loss_usdt: float,
    activation_trades: int, min_profit_factor: float,
) -> bool:
    if completed_trades < max(0, activation_trades) or profit_usdt <= loss_usdt:
        return False
    profit_factor = profit_usdt / loss_usdt if loss_usdt > 0 else math.inf
    return profit_factor >= max(0.0, min_profit_factor)


def _adverse_cut_for_leverage(
    *, leverage: int, spread_bps: float, fixed_cut_bps: float,
    spread_multiple: float, adverse_roe_pct: float,
) -> float:
    leverage_cut = adverse_roe_pct * 100.0 / max(1, leverage) if adverse_roe_pct > 0 else fixed_cut_bps
    return max(leverage_cut, spread_bps * spread_multiple)


def _update_leverage_normalized_trailing(
    trailing: PositiveTrailing, move_bps: float, *, leverage: int, reference_leverage: int = 200,
) -> float | None:
    scale = max(1, leverage) / max(1, reference_leverage)
    normalized_stop = trailing.update(move_bps * scale)
    return None if normalized_stop is None else normalized_stop / scale


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


def _marketable_demo_price(side: OrderSide, best: float, cross_bps: float, price_unit: float) -> float:
    if best <= 0 or price_unit <= 0:
        raise ValueError("best and price_unit must be positive")
    cross = Decimal(str(max(0.0, cross_bps))) / Decimal("10000")
    factor = Decimal("1") + cross if side is OrderSide.LONG else Decimal("1") - cross
    raw = Decimal(str(best)) * factor
    tick = Decimal(str(price_unit))
    rounding = ROUND_CEILING if side is OrderSide.LONG else ROUND_FLOOR
    return float((raw / tick).to_integral_value(rounding=rounding) * tick)


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
    "timestamp_ms", "excursion_id", "event", "symbol", "direction", "residual_bps", "threshold_bps",
    "net_margin_bps", "spread_bps", "binance_move_bps", "mexc_move_bps",
    "binance_age_ms", "mexc_age_ms", "reject_reason", "signal_to_order_latency_ms",
    "live_pnl_usdt", "move_bps", "mfe_bps", "mae_bps", "exit_reason", "hold_ms",
    "filled_qty", "effective_leverage", "live_notional_usdt",
    "demo_entry_fee_usdt", "demo_exit_fee_usdt", "demo_gross_pnl_usdt", "demo_net_pnl_usdt",
    "exit_residual_bps", "decision_demo_net_bps",
)

RESIDUAL_FIELDS = (
    "timestamp_ms", "symbol", "signal_source", "ready", "reason", "direction",
    "residual_bps", "threshold_bps", "raw_gap_bps", "baseline_gap_bps", "spread_bps",
    "binance_move_bps", "mexc_move_bps", "binance_age_ms", "mexc_age_ms", "demo_book_age_ms",
)


def _append_residual_sample(
    path: Path,
    *,
    timestamp_ms: int,
    symbol: str,
    signal_source: str,
    snapshot: MicroSpreadSnapshot,
    threshold_bps: float,
    spread_bps: float,
    demo_book_age_ms: float,
) -> None:
    """Persist throttled residual observations, including rejected sub-threshold states."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESIDUAL_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow({
            "timestamp_ms": int(timestamp_ms),
            "symbol": symbol,
            "signal_source": signal_source,
            "ready": int(snapshot.ready),
            "reason": snapshot.reason,
            "direction": snapshot.direction,
            "residual_bps": f"{snapshot.edge_bps:.9f}",
            "threshold_bps": f"{threshold_bps:.9f}",
            "raw_gap_bps": f"{snapshot.raw_gap_bps:.9f}",
            "baseline_gap_bps": f"{snapshot.baseline_gap_bps:.9f}",
            "spread_bps": f"{spread_bps:.9f}",
            "binance_move_bps": f"{snapshot.binance_move_bps:.9f}",
            "mexc_move_bps": f"{snapshot.mexc_move_bps:.9f}",
            "binance_age_ms": f"{snapshot.binance_age_ms:.3f}",
            "mexc_age_ms": f"{snapshot.mexc_age_ms:.3f}",
            "demo_book_age_ms": f"{demo_book_age_ms:.3f}",
        })


def _append_excursion(
    path: Path,
    candidate: MicroCandidate,
    *,
    timestamp_ms: int,
    excursion_id: str = "",
    event: str = "crossing",
    reject_reason: str = "",
    signal_to_order_latency_ms: float | None = None,
    live_pnl_usdt: float | None = None,
    move_bps: float | None = None,
    mfe_bps: float | None = None,
    mae_bps: float | None = None,
    exit_reason: str = "",
    hold_ms: float | None = None,
    filled_qty: float | None = None,
    effective_leverage: int | None = None,
    live_notional_usdt: float | None = None,
    demo_entry_fee_usdt: float | None = None,
    demo_exit_fee_usdt: float | None = None,
    demo_gross_pnl_usdt: float | None = None,
    demo_net_pnl_usdt: float | None = None,
    exit_residual_bps: float | None = None,
    decision_demo_net_bps: float | None = None,
) -> None:
    """Append one structured row for each hysteresis-consumed LIVE excursion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXCURSION_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow({
            "timestamp_ms": int(timestamp_ms),
            "excursion_id": excursion_id,
            "event": event,
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
            "reject_reason": reject_reason,
            "signal_to_order_latency_ms": "" if signal_to_order_latency_ms is None else f"{signal_to_order_latency_ms:.3f}",
            "live_pnl_usdt": "" if live_pnl_usdt is None else f"{live_pnl_usdt:.9f}",
            "move_bps": "" if move_bps is None else f"{move_bps:.9f}",
            "mfe_bps": "" if mfe_bps is None else f"{mfe_bps:.9f}",
            "mae_bps": "" if mae_bps is None else f"{mae_bps:.9f}",
            "exit_reason": exit_reason,
            "hold_ms": "" if hold_ms is None else f"{hold_ms:.3f}",
            "filled_qty": "" if filled_qty is None else f"{filled_qty:.12g}",
            "effective_leverage": "" if effective_leverage is None else str(effective_leverage),
            "live_notional_usdt": "" if live_notional_usdt is None else f"{live_notional_usdt:.9f}",
            "demo_entry_fee_usdt": "" if demo_entry_fee_usdt is None else f"{demo_entry_fee_usdt:.9f}",
            "demo_exit_fee_usdt": "" if demo_exit_fee_usdt is None else f"{demo_exit_fee_usdt:.9f}",
            "demo_gross_pnl_usdt": "" if demo_gross_pnl_usdt is None else f"{demo_gross_pnl_usdt:.9f}",
            "demo_net_pnl_usdt": "" if demo_net_pnl_usdt is None else f"{demo_net_pnl_usdt:.9f}",
            "exit_residual_bps": "" if exit_residual_bps is None else f"{exit_residual_bps:.9f}",
            "decision_demo_net_bps": "" if decision_demo_net_bps is None else f"{decision_demo_net_bps:.9f}",
        })


async def _discover_live_crosslisted_without_fee_gate() -> list[LiveZeroFeeContract]:
    binance_symbols = await fetch_binance_usdm_symbols()
    contracts = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()
    rows: list[LiveZeroFeeContract] = []
    for row in contracts:
        symbol = str(row.get("symbol") or "").upper()
        binance_symbol = mexc_to_binance_symbol(symbol)
        if not symbol or binance_symbol not in binance_symbols:
            continue
        rows.append(LiveZeroFeeContract(
            mexc_symbol=symbol,
            binance_symbol=binance_symbol,
            max_leverage=int(row.get("maxLeverage") or 1),
            contract_size=float(row.get("contractSize") or 0),
            min_vol=float(row.get("minVol") or 0),
        ))
    return rows


async def _discover_intersection(
    *, require_live_zero_fee: bool = True, require_demo_zero_fee: bool = True,
) -> list[DemoLiveContract]:
    live_rows = (
        await discover_live_zero_fee_crosslisted()
        if require_live_zero_fee else await _discover_live_crosslisted_without_fee_gate()
    )
    live_by_symbol = {row.mexc_symbol: row for row in live_rows}

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(demo_cfg) as adapter:
        demo_rows = await _fetch_contracts(adapter)
        demo_fees = await read_web_fee_provider(adapter)

    out: list[DemoLiveContract] = []
    for row in demo_rows:
        symbol = str(row.get("symbol") or "").upper()
        live = live_by_symbol.get(symbol)
        if live is not None and (
            not require_demo_zero_fee or _zero_fee_status(demo_fees.status(symbol))
        ):
            out.append(DemoLiveContract(live=live, demo=dict(row)))
    out.sort(key=lambda row: row.live.mexc_symbol)
    return out


async def _open_demo_ioc_with_leverage_fallback(
    adapter,
    *,
    symbol: str,
    side: OrderSide,
    price: float,
    min_base_qty: float,
    target_margin_usdt: float,
    leverage_cap: int,
    available_margin_usdt: float | None = None,
    max_base_qty: float = math.inf,
):
    """Retry only an explicit Testnet risk-tier rejection at lower leverage."""
    leverage = max(1, int(leverage_cap))
    attempted: set[int] = set()
    while leverage not in attempted:
        attempted.add(leverage)
        # Testnet charges taker fees even though the selected LIVE account is
        # gated to exact 0/0 fees. Reserve both Demo legs so a maximum-volume
        # entry can always be reduced-only closed.
        margin = (
            max(
                0.01,
                float(available_margin_usdt) * DEMO_BALANCE_SAFETY_FRACTION
                / (1.0 + leverage * DEMO_ROUND_TRIP_FEE_RATE),
            )
            if available_margin_usdt is not None
            else max(0.01, float(target_margin_usdt))
        )
        requested_qty = min(float(max_base_qty), max(
            float(min_base_qty),
            margin * leverage / float(price),
        ))
        try:
            fill = await adapter.open_ioc(
                symbol=symbol,
                side=side,
                price=price,
                qty=requested_qty,
                leverage=leverage,
                client_order_id=f"micro-{leverage}-{uuid.uuid4().hex}",
            )
            return fill, leverage
        except MexcWebError as exc:
            if "code=2005" in str(exc):
                return None, -1
            if "code=8819" not in str(exc):
                raise
            if leverage == 1:
                return None, 0
            leverage = max(1, leverage // 2)
    return None, 0


async def _demo_available_usdt(adapter) -> float:
    response = await adapter._request("GET", "/private/account/asset/USDT")  # noqa: SLF001
    data = response.get("data", response) if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        raise MexcWebError("Demo USDT asset payload is missing")
    available = float(data.get("availableBalance", data.get("available", 0)) or 0)
    if available <= 0:
        raise MexcWebError(f"Demo available USDT must be positive, got {available:g}")
    return available


async def _history_reconciled_fill(
    adapter,
    position: PositionSnapshot,
    fallback,
    *,
    entry_fee_usdt: float,
    timeout_seconds: float = 2.0,
):
    """Replace an eventually-consistent close response with authoritative position history."""
    if not hasattr(adapter, "_request"):
        return fallback
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        now_ms = int(time.time() * 1000)
        response = await adapter._request(  # noqa: SLF001
            "GET",
            "/private/position/list/history_positions",
            params={
                "symbol": position.symbol,
                "start_time": now_ms - 3_600_000,
                "end_time": now_ms + 60_000,
                "page_num": 1,
                "page_size": 100,
            },
        )
        data = response.get("data", response) if isinstance(response, dict) else {}
        rows = data if isinstance(data, list) else next(
            (data.get(key) for key in ("resultList", "list", "rows") if isinstance(data.get(key), list)),
            [],
        ) if isinstance(data, dict) else []
        history = next(
            (row for row in rows if str(row.get("positionId")) == str(position.position_id)),
            None,
        )
        if history is not None:
            total_fee = abs(float(history.get("totalFee", history.get("fee", 0)) or 0))
            return OrderFill(
                symbol=position.symbol,
                side=OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG,
                requested_qty=position.qty,
                filled_qty=position.qty,
                avg_price=float(history.get("closeAvgPrice") or position.entry_price),
                fee_usdt=max(0.0, total_fee - max(0.0, entry_fee_usdt)),
                order_id=(fallback.order_id if isinstance(fallback, OrderFill) else "history"),
                client_order_id=(fallback.client_order_id if isinstance(fallback, OrderFill) else "history"),
                position_id=position.position_id,
            )
        if time.monotonic() >= deadline:
            return fallback
        await asyncio.sleep(0.10)


async def _flatten_exact_demo_position(
    adapter,
    position: PositionSnapshot,
    reason: str,
    *,
    entry_fee_usdt: float = 0.0,
):
    """Close and reconcile one exact Testnet hedge leg by positionId."""
    if position.position_id is None:
        return await hybrid._flatten_position(adapter, position, reason)
    current = position
    last_fill = None
    for _ in range(4):
        try:
            last_fill = await adapter.close_position_snapshot_reduce_only(
                current,
                client_order_id=f"micro-exit-{uuid.uuid4().hex}",
            )
        except MexcWebError as exc:
            if "code=2009" not in str(exc):
                raise
            rows = await adapter.get_positions(current.symbol)
            residual = next((row for row in rows if row.position_id == current.position_id), None)
            if residual is not None:
                raise
            reconciled = await _history_reconciled_fill(
                adapter, position, last_fill, entry_fee_usdt=entry_fee_usdt,
            )
            if reconciled is not None:
                return reconciled
            raise
        deadline = time.monotonic() + EXACT_CLOSE_VISIBILITY_SECONDS
        while time.monotonic() < deadline:
            rows = await adapter.get_positions(current.symbol)
            residual = next((row for row in rows if row.position_id == current.position_id), None)
            if residual is None:
                return await _history_reconciled_fill(
                    adapter, position, last_fill, entry_fee_usdt=entry_fee_usdt,
                )
            current = residual
            await asyncio.sleep(0.08)
    raise MexcWebError(
        f"{reason}: exact Demo position remains after close positionId={position.position_id} qty={current.qty:g}"
    )


def _fee_gate_allows_entry(
    live_status,
    demo_status,
    *,
    require_live_zero_fee: bool,
    require_demo_zero_fee: bool = True,
) -> bool:
    return (
        not require_demo_zero_fee or _zero_fee_status(demo_status)
    ) and (
        not require_live_zero_fee or _zero_fee_status(live_status)
    )


async def _find_demo_position(adapter, symbol: str, side: OrderSide) -> PositionSnapshot | None:
    """Select the requested hedge leg and refuse an unexpected opposite leg."""
    rows = await adapter.get_positions(symbol)
    matching = next((row for row in rows if row.side is side), None)
    if matching is not None:
        return matching
    if rows:
        raise MexcWebError(
            f"Demo microspread position side mismatch: requested={side.value} "
            f"visible={','.join(row.side.value for row in rows)}"
        )
    return None


async def _wait_for_demo_position(
    adapter, symbol: str, side: OrderSide, *, timeout_seconds: float = 5.0,
) -> PositionSnapshot | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        position = await _find_demo_position(adapter, symbol, side)
        if position is not None:
            return position
        await asyncio.sleep(0.08)
    return None


async def run(args: argparse.Namespace) -> None:
    hybrid._load_project_env()
    if args.demo_zero_fee_only and args.allow_demo_fee_accounting:
        raise MexcWebError("--demo-zero-fee-only and --allow-demo-fee-accounting are mutually exclusive")
    require_live_zero_fee = not args.demo_zero_fee_only
    require_demo_zero_fee = not args.allow_demo_fee_accounting
    if (args.demo_zero_fee_only or args.allow_demo_fee_accounting) and not args.include_symbols.strip():
        raise MexcWebError("explicit fee experiment modes require an --include-symbols allowlist")
    intersection = await _discover_intersection(
        require_live_zero_fee=require_live_zero_fee,
        require_demo_zero_fee=require_demo_zero_fee,
    )
    if not intersection:
        raise MexcWebError(
            "no exact symbol exists in LIVE MEXC fee=0/0, Demo MEXC fee=0/0 and Binance USD-M simultaneously"
        )

    excursion_csv = Path(args.excursion_csv) if args.excursion_csv else Path(
        f"microspread_excursions_{int(time.time())}.csv"
    )
    if args.residual_csv:
        residual_csv = Path(args.residual_csv)
    elif args.excursion_csv:
        residual_csv = excursion_csv.with_name(f"{excursion_csv.stem}_residuals{excursion_csv.suffix}")
    else:
        residual_csv = excursion_csv.with_name(
            excursion_csv.name.replace("microspread_excursions_", "microspread_residuals_", 1)
        )

    contracts = [row.live for row in intersection]
    symbols = [row.live.mexc_symbol for row in intersection]
    included = {
        item.strip().upper()
        for item in str(args.include_symbols or "").split(",")
        if item.strip()
    }
    if included:
        contracts = [row for row in contracts if row.mexc_symbol in included]
        symbols = [symbol for symbol in symbols if symbol in included]
    excluded = {
        item.strip().upper()
        for item in str(args.exclude_symbols or "").split(",")
        if item.strip()
    }
    if args.max_demo_volume:
        excluded.update(MAX_VOLUME_DEMO_QUARANTINE)
    if excluded:
        contracts = [row for row in contracts if row.mexc_symbol not in excluded]
        symbols = [symbol for symbol in symbols if symbol not in excluded]
        hybrid.console.print("Demo execution quarantine: " + ", ".join(sorted(excluded)))
    if not symbols:
        raise MexcWebError("all Demo/LIVE intersection symbols were excluded")
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

    signal_from_demo = args.signal_mexc_source == "demo"
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, None if signal_from_demo else models, wake)
    demo_wake = asyncio.Event()
    demo_books = EventMexcDepthFeed(
        symbols,
        models if signal_from_demo else None,
        wake if signal_from_demo else demo_wake,
        ws_url=DEMO_WS,
    )
    signal_books = demo_books if signal_from_demo else mexc
    await binance.start()
    if not signal_from_demo:
        await mexc.start()
    await demo_books.start()

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(demo_cfg)
    live_fee_cfg = WebExecutionConfig.from_env(write_enabled=False)

    hybrid.console.print(
        f"[cyan]LIVE MICROSPREAD -> DEMO[/cyan]: {len(symbols)} symbol(s); event-driven Binance bookTicker + "
        f"MEXC {args.signal_mexc_source.upper()} depth; all order writes TESTNET only."
    )
    hybrid.console.print(
        "Fee gate: "
        + ("Demo exact 0/0 + " if require_demo_zero_fee else "Demo fees measured/subtracted + ")
        + ("LIVE exact 0/0" if require_live_zero_fee else "explicit Demo-only experiment")
    )
    hybrid.console.print(f"Excursion telemetry CSV: {excursion_csv.resolve()}")
    hybrid.console.print(f"Residual telemetry CSV: {residual_csv.resolve()}")
    hybrid.console.print("Symbols: " + ", ".join(symbols))
    hybrid.console.print(
        f"Micro gate: residual >= max({args.min_edge_bps:.2f}bps, LIVE spread+{args.min_net_edge_bps:.2f}bps, "
        f"spread*{args.edge_to_spread_ratio:.2f}); Binance micro-move >= {args.min_binance_move_bps:.3f}bps; "
        f"basis baseline={args.baseline_seconds:g}s excluding newest {args.baseline_exclusion_ms}ms."
    )

    demo_meta: dict[str, tuple[float, int, float, float]] = {}
    demo_available_usdt = 0.0
    live_fee_provider = None
    demo_fee_provider = None
    fee_checked_ms = 0
    next_fee_refresh = 0.0
    next_heartbeat = 0.0
    warmup_until = time.monotonic() + float(args.warmup_seconds)
    deadline = time.monotonic() + float(args.session_seconds)
    last_entry_ms = {symbol: -10**18 for symbol in symbols}
    last_residual_sample_ms = {symbol: -10**18 for symbol in symbols}

    position: PositionSnapshot | None = None
    position_symbol = ""
    position_direction = 0
    live_entry_price = 0.0
    live_entry_edge_bps = 0.0
    live_entry_spread_bps = 0.0
    live_entry_leverage = 1
    live_entry_notional_usdt = 0.0
    demo_entry_fee_usdt = 0.0
    entry_time = 0.0
    live_mfe_bps = 0.0
    live_mae_bps = 0.0
    demo_net_mfe_bps = 0.0
    demo_net_mae_bps = 0.0
    trailing: PositiveTrailing | None = None
    position_candidate: MicroCandidate | None = None
    position_excursion_id = ""

    cycles = signals = wins = losses = 0
    total_live_pnl = 0.0
    gross_profit = gross_loss = 0.0
    peak_pnl = max_drawdown = 0.0
    excursions_seen = 0
    last_excursion_key: tuple[str, int] | None = None
    pending_confirmation: tuple[str, int, int] | None = None
    strategy_equity_usdt = max(0.01, float(args.strategy_bankroll_usdt))
    sizing_profit_usdt = sizing_loss_usdt = 0.0

    try:
        async with _FastLeadLagDemoAdapter(demo_cfg) as demo_adapter, MexcWebExecutionAdapter(live_fee_cfg) as live_fee_adapter:
            demo_available_usdt = await _demo_available_usdt(demo_adapter)
            for symbol in symbols:
                detail = await demo_adapter.get_contract_detail(symbol)
                contract_size = float(detail.get("contractSize") or 0)
                min_vol = float(detail.get("minVol") or 0)
                max_lev = int(detail.get("maxLeverage") or 1)
                max_vol = float(detail.get("maxVol") or math.inf)
                price_unit = float(detail.get("priceUnit") or 0)
                if contract_size > 0 and min_vol > 0 and price_unit > 0:
                    demo_meta[symbol] = (
                        contract_size * min_vol, max_lev, contract_size * max_vol, price_unit,
                    )
            symbols = [symbol for symbol in symbols if symbol in demo_meta]
            if not symbols:
                raise MexcWebError("no Demo contract has valid sizing metadata")
            hybrid.console.print(
                f"Demo sizing: available={demo_available_usdt:g}USDT mode="
                f"{'MAX isolated volume' if args.max_demo_volume else f'{args.target_margin_usdt:g}USDT margin cap'}"
            )

            live_fee_provider = await read_web_fee_provider(live_fee_adapter)
            demo_fee_provider = await read_web_fee_provider(demo_adapter)
            fee_checked_ms = int(time.time() * 1000)
            next_fee_refresh = time.monotonic() + FEE_REFRESH_SECONDS

            while time.monotonic() < deadline and cycles < int(args.max_cycles):
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                if now >= next_fee_refresh:
                    live_fee_provider = await read_web_fee_provider(live_fee_adapter)
                    demo_fee_provider = await read_web_fee_provider(demo_adapter)
                    fee_checked_ms = int(time.time() * 1000)
                    next_fee_refresh = time.monotonic() + FEE_REFRESH_SECONDS

                if position is None:
                    candidates: list[MicroCandidate] = []
                    max_residual = 0.0
                    above_floor = 0
                    if now >= warmup_until and live_fee_provider is not None and demo_fee_provider is not None:
                        for symbol in symbols:
                            book = signal_books.books.get(symbol)
                            if book is None:
                                continue
                            book_age = now_ms - book.recv_ms
                            if book_age < 0 or book_age > float(args.max_book_age_ms):
                                continue
                            live_status = live_fee_provider.status(symbol)
                            demo_status = demo_fee_provider.status(symbol)
                            if (
                                not _fee_gate_allows_entry(
                                    live_status, demo_status,
                                    require_live_zero_fee=require_live_zero_fee,
                                    require_demo_zero_fee=require_demo_zero_fee,
                                )
                                or now_ms - fee_checked_ms > FEE_MAX_AGE_MS
                            ):
                                continue
                            if now_ms - last_entry_ms[symbol] < int(args.entry_cooldown_ms):
                                continue

                            raw = models[symbol].snapshot(now_ms=now_ms, threshold_bps=args.min_edge_bps)
                            max_residual = max(max_residual, abs(raw.edge_bps))
                            above_floor += int(abs(raw.edge_bps) >= args.min_edge_bps)

                            demo_book = demo_books.books.get(symbol)
                            demo_book_age = now_ms - demo_book.recv_ms if demo_book is not None else math.inf
                            if now_ms - last_residual_sample_ms[symbol] >= int(args.residual_sample_ms):
                                threshold = _required_edge(
                                    spread_bps=book.spread_bps,
                                    min_edge_bps=args.min_edge_bps,
                                    min_net_edge_bps=args.min_net_edge_bps,
                                    spread_ratio=args.edge_to_spread_ratio,
                                )
                                _append_residual_sample(
                                    residual_csv,
                                    timestamp_ms=now_ms,
                                    symbol=symbol,
                                    signal_source=args.signal_mexc_source,
                                    snapshot=raw,
                                    threshold_bps=threshold,
                                    spread_bps=book.spread_bps,
                                    demo_book_age_ms=demo_book_age,
                                )
                                last_residual_sample_ms[symbol] = now_ms
                            if demo_book is None or demo_book_age < 0 or demo_book_age > float(args.max_book_age_ms):
                                continue

                            candidate = _candidate_from_model(symbol, models[symbol], book, args, now_ms=now_ms)
                            if candidate is not None:
                                candidates.append(candidate)

                    best = _best_candidate(candidates)
                    confirmed, pending_confirmation = _confirmed_candidate(
                        pending_confirmation,
                        best,
                        now_ms=now_ms,
                        confirm_ms=args.entry_confirm_ms,
                    )
                    if not confirmed:
                        best = None
                    if best is not None:
                        # Consume the hysteresis crossing only for the winner.
                        consumed = _candidate_from_model(
                            best.symbol, models[best.symbol], best.book, args, now_ms=now_ms, consume=True
                        )
                        if consumed is not None:
                            excursion_id = uuid.uuid4().hex
                            signal_timestamp_ms = int(time.time() * 1000)
                            _append_excursion(
                                excursion_csv, consumed, timestamp_ms=signal_timestamp_ms,
                                excursion_id=excursion_id,
                            )
                            excursion_key = (consumed.symbol, consumed.direction)
                            if excursion_key != last_excursion_key:
                                excursions_seen += 1
                                last_excursion_key = excursion_key

                            side = OrderSide.LONG if consumed.direction > 0 else OrderSide.SHORT
                            live_status = live_fee_provider.status(consumed.symbol)
                            demo_status = demo_fee_provider.status(consumed.symbol)
                            if (
                                _fee_gate_allows_entry(
                                    live_status, demo_status,
                                    require_live_zero_fee=require_live_zero_fee,
                                    require_demo_zero_fee=require_demo_zero_fee,
                                )
                                and now_ms - fee_checked_ms <= FEE_MAX_AGE_MS
                            ):
                                # Capture the LIVE executable entry before any Demo REST call.
                                live_entry = consumed.book.ask if consumed.direction > 0 else consumed.book.bid

                                demo_book = demo_books.books.get(consumed.symbol)
                                if (
                                    demo_book is None
                                    or signal_timestamp_ms - demo_book.recv_ms < 0
                                    or signal_timestamp_ms - demo_book.recv_ms > float(args.max_book_age_ms)
                                ):
                                    _append_excursion(
                                        excursion_csv, consumed, timestamp_ms=int(time.time() * 1000),
                                        excursion_id=excursion_id, event="rejected",
                                        reject_reason="demo_book_unavailable",
                                    )
                                    continue
                                demo_ask, demo_bid = demo_book.ask, demo_book.bid
                                if demo_ask > demo_bid > 0:
                                    # Never chase an excursion that vanished between crossing and IOC preparation.
                                    fresh_now_ms = int(time.time() * 1000)
                                    fresh_book = signal_books.books.get(consumed.symbol)
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
                                        min_base_qty, max_lev, max_base_qty, price_unit = demo_meta[consumed.symbol]
                                        demo_best = _marketable_demo_price(
                                            side,
                                            demo_ask if side is OrderSide.LONG else demo_bid,
                                            args.demo_ioc_cross_bps,
                                            price_unit,
                                        )
                                        leverage = min(max(1, int(args.leverage)), max_lev)
                                        dynamic_sizing = _dynamic_sizing_ready(
                                            completed_trades=cycles,
                                            profit_usdt=sizing_profit_usdt,
                                            loss_usdt=sizing_loss_usdt,
                                            activation_trades=args.sizing_activation_trades,
                                            min_profit_factor=args.sizing_min_profit_factor,
                                        )
                                        cycle_margin_usdt = _cycle_margin_usdt(
                                            fixed_margin_usdt=args.target_margin_usdt,
                                            strategy_equity_usdt=strategy_equity_usdt,
                                            target_exposure_multiple=(
                                                args.target_exposure_equity_multiple if dynamic_sizing else 0.0
                                            ),
                                            leverage=leverage,
                                            target_notional_usdt=args.target_notional_usdt,
                                        )
                                        signals += 1
                                        order_request_monotonic = time.monotonic()
                                        order_request_ms = int(time.time() * 1000)
                                        _append_excursion(
                                            excursion_csv, fresh, timestamp_ms=order_request_ms,
                                            excursion_id=excursion_id, event="demo_ioc_request",
                                            signal_to_order_latency_ms=order_request_ms - signal_timestamp_ms,
                                        )
                                        hybrid.console.print(
                                            f"MICRO SIGNAL {consumed.symbol} {'LONG' if consumed.direction > 0 else 'SHORT'} "
                                            f"residual={fresh.edge_bps:+.3f}bps threshold={fresh.threshold_bps:.3f} "
                                            f"spread={fresh.spread_bps:.3f} net={fresh.net_margin_bps:+.3f} "
                                            f"B100={fresh.binance_move_bps:+.3f} M100={fresh.mexc_move_bps:+.3f} "
                                            f"Bage={fresh.snapshot.binance_age_ms:.0f}ms Mage={fresh.snapshot.mexc_age_ms:.0f}ms "
                                            f"DemoCross={args.demo_ioc_cross_bps:.1f}bps"
                                        )
                                        fill, leverage = await _open_demo_ioc_with_leverage_fallback(
                                            demo_adapter,
                                            symbol=consumed.symbol,
                                            side=side,
                                            price=demo_best,
                                            min_base_qty=min_base_qty,
                                            target_margin_usdt=cycle_margin_usdt,
                                            leverage_cap=leverage,
                                            available_margin_usdt=(demo_available_usdt if args.max_demo_volume else None),
                                            max_base_qty=max_base_qty,
                                        )
                                        if fill is None:
                                            _append_excursion(
                                                excursion_csv, fresh, timestamp_ms=int(time.time() * 1000),
                                                excursion_id=excursion_id, event="rejected",
                                                reject_reason=(
                                                    "demo_balance_insufficient"
                                                    if leverage < 0 else "demo_leverage_unavailable"
                                                ),
                                                signal_to_order_latency_ms=order_request_ms - signal_timestamp_ms,
                                            )
                                            continue
                                        response_ms = int(time.time() * 1000)
                                        _append_excursion(
                                            excursion_csv, fresh, timestamp_ms=response_ms,
                                            excursion_id=excursion_id, event="demo_ioc_response",
                                            signal_to_order_latency_ms=order_request_ms - signal_timestamp_ms,
                                        )
                                        remote = await _find_demo_position(demo_adapter, consumed.symbol, side)
                                        if remote is None and (fill.order_id or fill.position_id or fill.filled_qty > 0):
                                            remote = await _wait_for_demo_position(demo_adapter, consumed.symbol, side)
                                        if remote is None:
                                            _append_excursion(
                                                excursion_csv, fresh, timestamp_ms=int(time.time() * 1000),
                                                excursion_id=excursion_id, event="rejected",
                                                reject_reason="demo_ioc_unfilled_or_pending",
                                                signal_to_order_latency_ms=order_request_ms - signal_timestamp_ms,
                                            )
                                            stable_flat = await wait_account_flat(
                                                demo_adapter,
                                                stable_seconds=3.0,
                                                timeout_seconds=AMBIGUOUS_IOC_FLAT_TIMEOUT_SECONDS,
                                            )
                                            if not stable_flat:
                                                raise MexcWebError(
                                                    "ambiguous Demo IOC state: account did not remain flat; "
                                                    "blocking all new entries"
                                                )
                                        if remote is not None:
                                            if remote.side is not side:
                                                raise MexcWebError("Demo microspread position side mismatch")
                                            if require_demo_zero_fee and fill.fee_usdt != 0.0:
                                                emergency_fill = await _flatten_exact_demo_position(
                                                    demo_adapter, remote, "demo_entry_fee_violation"
                                                )
                                                _append_excursion(
                                                    excursion_csv, fresh, timestamp_ms=int(time.time() * 1000),
                                                    excursion_id=excursion_id, event="rejected",
                                                    reject_reason="demo_nonzero_fee_observed",
                                                )
                                                raise MexcWebError(
                                                    "strict Demo zero-fee gate violated on entry: "
                                                    f"entry_fee={fill.fee_usdt:g} exit_fee={emergency_fill.fee_usdt:g}"
                                                )
                                            position = remote
                                            position_symbol = consumed.symbol
                                            position_direction = consumed.direction
                                            live_entry_price = live_entry
                                            live_entry_edge_bps = fresh.edge_bps
                                            live_entry_spread_bps = fresh.spread_bps
                                            live_entry_leverage = leverage
                                            live_entry_notional_usdt = remote.qty * live_entry_price
                                            demo_entry_fee_usdt = fill.fee_usdt
                                            entry_time = order_request_monotonic
                                            live_mfe_bps = live_mae_bps = 0.0
                                            demo_net_mfe_bps = demo_net_mae_bps = 0.0
                                            trailing_scale = leverage / 200.0
                                            trailing = PositiveTrailing(
                                                distance_bps=max(
                                                    args.trailing_distance_bps,
                                                    live_entry_spread_bps * trailing_scale,
                                                )
                                            )
                                            position_candidate = fresh
                                            position_excursion_id = excursion_id
                                            last_entry_ms[position_symbol] = int(time.time() * 1000)
                                            hybrid.console.print(
                                                f"DEMO ENTRY {position_symbol} {'LONG' if position_direction > 0 else 'SHORT'} "
                                                f"qty={remote.qty:g} LIVEEntry={live_entry_price:g} residual={live_entry_edge_bps:+.3f} "
                                                f"notional={live_entry_notional_usdt:g}USDT leverage={leverage}x "
                                                f"requested_margin={cycle_margin_usdt:.4f}USDT strategy_equity={strategy_equity_usdt:.4f}USDT "
                                                f"sizing={('TARGET_NOTIONAL' if args.target_notional_usdt > 0 else ('DYNAMIC' if dynamic_sizing else 'PROBATION'))} "
                                                f"spread={live_entry_spread_bps:.3f} DemoFeeReported={fill.fee_usdt:g}"
                                            )

                    if position is None and now >= next_heartbeat:
                        phase = "warming" if now < warmup_until else "watching"
                        hybrid.console.print(
                            f"MICRO HEARTBEAT state={phase} symbols={len(symbols)} "
                            f"signal_source={args.signal_mexc_source} books={len(signal_books.books)} "
                            f"demo_books={len(demo_books.books)} Bquotes={binance.quotes} Mdepth={mexc.updates} "
                            f"Ddepth={demo_books.updates} above_floor={above_floor} "
                            f"max_residual={max_residual:.3f}bps candidates={len(candidates)} "
                            f"fee_age={max(0, now_ms-fee_checked_ms)}ms"
                        )
                        next_heartbeat = now + float(args.heartbeat_seconds)

                else:
                    assert position_symbol and position_direction in (-1, 1) and trailing is not None
                    book = signal_books.books.get(position_symbol)
                    demo_book = demo_books.books.get(position_symbol)
                    if book is not None and demo_book is not None:
                        live_exit = book.bid if position_direction > 0 else book.ask
                        move_bps = _signed_move_bps(position_direction, live_entry_price, live_exit)
                        live_mfe_bps = max(live_mfe_bps, move_bps)
                        live_mae_bps = min(live_mae_bps, move_bps)
                        demo_exit_mark = demo_book.bid if position_direction > 0 else demo_book.ask
                        current_demo_fee = demo_fee_provider.status(position_symbol)
                        demo_exit_fee_rate = float(current_demo_fee.taker or 0.0)
                        demo_mark_gross, demo_mark_net, demo_mark_net_bps = _estimated_demo_net_bps(
                            direction=position_direction,
                            entry_price=position.entry_price,
                            exit_price=demo_exit_mark,
                            qty=position.qty,
                            entry_fee_usdt=demo_entry_fee_usdt,
                            exit_fee_rate=demo_exit_fee_rate,
                        )
                        demo_entry_notional = position.entry_price * position.qty
                        demo_mark_gross_bps = (
                            demo_mark_gross / demo_entry_notional * 10_000.0
                            if demo_entry_notional > 0 else -math.inf
                        )
                        demo_net_mfe_bps = max(demo_net_mfe_bps, demo_mark_net_bps)
                        demo_net_mae_bps = min(demo_net_mae_bps, demo_mark_net_bps)
                        trail = _update_leverage_normalized_trailing(
                            trailing, demo_mark_net_bps, leverage=live_entry_leverage,
                        )
                        age_s = now - entry_time
                        snap = models[position_symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)

                        reason: str | None = None
                        if trail is not None and demo_mark_net_bps <= trail and age_s >= args.min_hold_seconds:
                            reason = "positive_trailing_stop"
                        adverse = _adverse_cut_for_leverage(
                            leverage=live_entry_leverage,
                            spread_bps=live_entry_spread_bps,
                            fixed_cut_bps=args.adverse_cut_bps,
                            spread_multiple=args.adverse_spread_mult,
                            adverse_roe_pct=args.adverse_cut_roe_pct,
                        )
                        if reason is None and demo_mark_gross_bps <= -adverse and age_s >= args.min_hold_seconds:
                            reason = "demo_adverse_cut"
                        if (
                            reason is None
                            and _convergence_exit_allowed(
                                current_edge_bps=snap.edge_bps,
                                entry_edge_bps=live_entry_edge_bps,
                                convergence_bps=args.convergence_bps,
                                convergence_fraction=args.convergence_fraction,
                                demo_net_bps=demo_mark_net_bps,
                                min_exit_profit_bps=args.min_exit_profit_bps,
                            )
                        ):
                            reason = "microspread_converged"
                        assert position_candidate is not None
                        if reason is None and _profitable_reversal_exit_allowed(
                            current_direction=snap.direction,
                            position_direction=position_direction,
                            current_edge_bps=snap.edge_bps,
                            entry_threshold_bps=position_candidate.threshold_bps,
                            reversal_edge_bps=args.reversal_edge_bps,
                            demo_net_bps=demo_mark_net_bps,
                            min_exit_profit_bps=args.min_exit_profit_bps,
                        ):
                            reason = "microspread_reversed"
                        if (
                            reason is None
                            and args.binance_reversal_exit_bps > 0
                            and snap.binance_move_bps * position_direction <= -args.binance_reversal_exit_bps
                            and age_s >= args.min_hold_seconds
                        ):
                            reason = "binance_micro_reversal"
                        if reason is None and args.max_hold_seconds > 0 and age_s >= args.max_hold_seconds:
                            reason = "microspread_timeout"

                        if now >= next_heartbeat:
                            trail_txt = "OFF" if trail is None else f"+{trail:.3f}bps"
                            hybrid.console.print(
                                f"MICRO POSITION {position_symbol} mark={move_bps:+.3f}bps MFE={live_mfe_bps:+.3f} "
                                f"MAE={live_mae_bps:+.3f} DemoNetMark={demo_mark_net:+.6f}USDT "
                                f"DemoNet={demo_mark_net_bps:+.3f}bps DemoMFE={demo_net_mfe_bps:+.3f} "
                                f"DemoMAE={demo_net_mae_bps:+.3f} TRAIL={trail_txt} residual={snap.edge_bps:+.3f}bps "
                                f"B100={snap.binance_move_bps:+.3f}"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)

                        if reason is not None:
                            demo_fill = await _flatten_exact_demo_position(
                                demo_adapter, position, reason, entry_fee_usdt=demo_entry_fee_usdt,
                            )
                            live_pnl = live_entry_notional_usdt * move_bps / 10_000.0
                            demo_gross_pnl = (
                                position_direction * (demo_fill.avg_price - position.entry_price) * position.qty
                            )
                            demo_net_pnl = demo_gross_pnl - demo_entry_fee_usdt - demo_fill.fee_usdt
                            strategy_equity_usdt = max(0.01, strategy_equity_usdt + demo_net_pnl)
                            if demo_net_pnl > 0:
                                sizing_profit_usdt += demo_net_pnl
                            elif demo_net_pnl < 0:
                                sizing_loss_usdt += abs(demo_net_pnl)
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
                            assert position_candidate is not None
                            _append_excursion(
                                excursion_csv, position_candidate, timestamp_ms=int(time.time() * 1000),
                                excursion_id=position_excursion_id, event="demo_exit",
                                live_pnl_usdt=live_pnl, move_bps=move_bps,
                                mfe_bps=live_mfe_bps, mae_bps=live_mae_bps,
                                exit_reason=reason, hold_ms=age_s * 1000.0,
                                filled_qty=position.qty, effective_leverage=live_entry_leverage,
                                live_notional_usdt=live_entry_notional_usdt,
                                demo_entry_fee_usdt=demo_entry_fee_usdt,
                                demo_exit_fee_usdt=demo_fill.fee_usdt,
                                demo_gross_pnl_usdt=demo_gross_pnl,
                                demo_net_pnl_usdt=demo_net_pnl,
                                exit_residual_bps=snap.edge_bps,
                                decision_demo_net_bps=demo_mark_net_bps,
                            )
                            hybrid.console.print(
                                f"MICRO EXIT {position_symbol} reason={reason} LIVEpnl={live_pnl:+.6f}USDT "
                                f"move={move_bps:+.3f}bps MFE={live_mfe_bps:+.3f} MAE={live_mae_bps:+.3f} "
                                f"hold={age_s * 1000.0:.0f}ms DemoExit={demo_fill.avg_price:g} "
                                f"DemoGross={demo_gross_pnl:+.6f} DemoNet={demo_net_pnl:+.6f} "
                                f"DemoFee={demo_fill.fee_usdt:g}"
                            )
                            if require_demo_zero_fee and demo_fill.fee_usdt != 0.0:
                                raise MexcWebError(
                                    f"strict Demo zero-fee gate violated on exit: fee={demo_fill.fee_usdt:g}"
                                )
                            if args.max_demo_volume:
                                demo_available_usdt = await _demo_available_usdt(demo_adapter)
                            position = None
                            position_symbol = ""
                            position_direction = 0
                            live_entry_price = live_entry_edge_bps = live_entry_spread_bps = 0.0
                            live_entry_leverage = 1
                            live_entry_notional_usdt = 0.0
                            demo_entry_fee_usdt = 0.0
                            entry_time = live_mfe_bps = live_mae_bps = 0.0
                            demo_net_mfe_bps = demo_net_mae_bps = 0.0
                            trailing = None
                            position_candidate = None
                            position_excursion_id = ""

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
                book = signal_books.books.get(position_symbol)
                live_exit = (book.bid if position_direction > 0 else book.ask) if book else live_entry_price
                move_bps = _signed_move_bps(position_direction, live_entry_price, live_exit)
                demo_fill = await _flatten_exact_demo_position(
                    demo_adapter, position, "session_end", entry_fee_usdt=demo_entry_fee_usdt,
                )
                live_pnl = live_entry_notional_usdt * move_bps / 10_000.0
                demo_gross_pnl = position_direction * (demo_fill.avg_price - position.entry_price) * position.qty
                demo_net_pnl = demo_gross_pnl - demo_entry_fee_usdt - demo_fill.fee_usdt
                total_live_pnl += live_pnl
                cycles += 1
                assert position_candidate is not None
                _append_excursion(
                    excursion_csv, position_candidate, timestamp_ms=int(time.time() * 1000),
                    excursion_id=position_excursion_id, event="demo_exit",
                    live_pnl_usdt=live_pnl, move_bps=move_bps,
                    mfe_bps=live_mfe_bps, mae_bps=live_mae_bps,
                    exit_reason="session_end", hold_ms=(time.monotonic() - entry_time) * 1000.0,
                    filled_qty=position.qty, effective_leverage=live_entry_leverage,
                    live_notional_usdt=live_entry_notional_usdt,
                    demo_entry_fee_usdt=demo_entry_fee_usdt,
                    demo_exit_fee_usdt=demo_fill.fee_usdt,
                    demo_gross_pnl_usdt=demo_gross_pnl,
                    demo_net_pnl_usdt=demo_net_pnl,
                )
                hybrid.console.print(
                    f"SESSION FLATTEN {position_symbol} LIVEpnl={live_pnl:+.6f}USDT DemoExit={demo_fill.avg_price:g}"
                )
    finally:
        await binance.close()
        await mexc.close()
        await demo_books.close()

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
    parser.add_argument(
        "--target-notional-usdt", type=float, default=0.0,
        help="request this IOC notional regardless of leverage; 0 keeps margin/equity sizing",
    )
    parser.add_argument("--strategy-bankroll-usdt", type=float, default=60.0)
    parser.add_argument(
        "--target-exposure-equity-multiple", type=float, default=0.0,
        help="size requested notional as this multiple of strategy equity; 0 uses fixed target margin",
    )
    parser.add_argument("--sizing-activation-trades", type=int, default=0)
    parser.add_argument("--sizing-min-profit-factor", type=float, default=1.2)
    parser.add_argument("--max-demo-volume", action="store_true")
    parser.add_argument("--demo-ioc-cross-bps", type=float, default=5.0)
    parser.add_argument("--exclude-symbols", default="")
    parser.add_argument("--include-symbols", default="")
    parser.add_argument("--demo-zero-fee-only", action="store_true")
    parser.add_argument("--allow-demo-fee-accounting", action="store_true")
    parser.add_argument(
        "--signal-mexc-source", choices=("live", "demo"), default="live",
        help="MEXC depth used for residual/convergence; demo aligns signals with Demo execution",
    )
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
    parser.add_argument(
        "--entry-confirm-ms", type=int, default=0,
        help="require an executable residual to persist before consuming the excursion and sending IOC",
    )
    parser.add_argument("--heartbeat-seconds", type=float, default=2.0)
    parser.add_argument("--idle-timeout-seconds", type=float, default=0.05)
    parser.add_argument("--min-hold-seconds", type=float, default=0.05)
    parser.add_argument(
        "--max-hold-seconds", type=float, default=0.0,
        help="hard position timeout; 0 disables it so convergence/protective exits control the hold",
    )
    parser.add_argument("--adverse-cut-bps", type=float, default=1.5)
    parser.add_argument("--adverse-spread-mult", type=float, default=1.25)
    parser.add_argument(
        "--adverse-cut-roe-pct", type=float, default=0.0,
        help="leverage-normalized adverse cut in margin ROE percent; 0 uses fixed bps",
    )
    parser.add_argument("--convergence-bps", type=float, default=0.10)
    parser.add_argument("--convergence-fraction", type=float, default=0.0)
    parser.add_argument(
        "--min-exit-profit-bps", type=float, default=0.5,
        help="minimum estimated Demo net profit required for a convergence exit",
    )
    parser.add_argument("--reversal-edge-bps", type=float, default=0.20)
    parser.add_argument(
        "--binance-reversal-exit-bps", type=float, default=0.0,
        help="opposite Binance micro-move exit threshold; 0 disables this noise-sensitive exit",
    )
    parser.add_argument("--trailing-distance-bps", type=float, default=1.0)
    parser.add_argument("--max-session-loss-usdt", type=float, default=0.50)
    parser.add_argument("--excursion-csv", default="")
    parser.add_argument("--residual-csv", default="")
    parser.add_argument("--residual-sample-ms", type=int, default=100)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        try:
            asyncio.run(flatten_all_demo_positions(reason="microspread-failure", quiet_if_flat=True))
        except Exception as cleanup_exc:
            hybrid.console.print(f"[red]MICROSPREAD EMERGENCY CLEANUP FAILED:[/red] {cleanup_exc}")
        hybrid.console.print(f"[red]MICROSPREAD DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
