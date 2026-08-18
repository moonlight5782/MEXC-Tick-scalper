from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum

from .execution import OrderFill, OrderSide, PositionSnapshot
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig


class ExecutionState(str, Enum):
    FLAT = "flat"
    ENTRY_PENDING = "entry_pending"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    RECONCILING = "reconciling"


@dataclass(frozen=True, slots=True)
class RiskCap:
    symbol: str
    position_type: int
    max_vol: float
    max_leverage: int
    mmr: float
    imr: float


@dataclass(slots=True)
class ExecutionTiming:
    ioc_post_start_ms: float = 0.0
    ioc_post_response_ms: float = 0.0
    ioc_confirmed_ms: float = 0.0
    position_visible_ms: float = 0.0
    close_post_start_ms: float = 0.0
    close_confirmed_ms: float = 0.0
    flat_visible_ms: float = 0.0

    @property
    def entry_post_to_confirm_ms(self) -> float:
        return max(0.0, self.ioc_confirmed_ms - self.ioc_post_start_ms)

    @property
    def entry_post_to_visible_ms(self) -> float:
        return max(0.0, self.position_visible_ms - self.ioc_post_start_ms)

    @property
    def close_post_to_confirm_ms(self) -> float:
        return max(0.0, self.close_confirmed_ms - self.close_post_start_ms)

    @property
    def close_post_to_flat_ms(self) -> float:
        return max(0.0, self.flat_visible_ms - self.close_post_start_ms)


@dataclass(frozen=True, slots=True)
class OpenResult:
    fill: OrderFill
    position: PositionSnapshot | None
    timing: ExecutionTiming


def _position_type(side: OrderSide) -> int:
    return 1 if side is OrderSide.LONG else 2


def parse_risk_limits(response: object) -> dict[tuple[str, int], RiskCap]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data", {})
    rows: list[dict] = []
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                rows.extend(x for x in value if isinstance(x, dict))
            elif isinstance(value, dict):
                rows.append(value)
    elif isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]

    out: dict[tuple[str, int], RiskCap] = {}
    for row in rows:
        try:
            symbol = str(row.get("symbol") or "").upper()
            ptype = int(row.get("positionType") or 0)
            cap = RiskCap(
                symbol=symbol,
                position_type=ptype,
                max_vol=float(row.get("maxVol") or 0),
                max_leverage=int(row.get("maxLeverage") or 0),
                mmr=float(row.get("mmr") or 0),
                imr=float(row.get("imr") or 0),
            )
        except (TypeError, ValueError):
            continue
        if symbol and ptype in (1, 2):
            out[(symbol, ptype)] = cap
    return out


class CanonicalTestnetExecution:
    """Single-position MEXC Testnet execution engine.

    It never targets LIVE: configuration is created only through demo_from_env(),
    which hard-rejects non-testnet hosts. State locks cover only state transitions;
    network I/O is deliberately outside the lock. There is no IOC top-up/chase.
    """

    def __init__(self, *, leverage: int = 10, reconcile_timeout_s: float = 2.0, poll_ms: float = 25.0) -> None:
        self.leverage = max(1, int(leverage))
        self.reconcile_timeout_s = max(0.2, float(reconcile_timeout_s))
        self.poll_s = max(0.01, float(poll_ms) / 1000.0)
        self.adapter = MexcWebExecutionAdapter(WebExecutionConfig.demo_from_env(write_enabled=True))
        self.state = ExecutionState.FLAT
        self.position: PositionSnapshot | None = None
        self._lock = asyncio.Lock()
        self._risk_caps: dict[tuple[str, int], RiskCap] = {}
        self._disabled: set[tuple[str, int]] = set()
        self._details: dict[str, dict] = {}

    async def __aenter__(self) -> "CanonicalTestnetExecution":
        await self.adapter.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.adapter.__aexit__(exc_type, exc, tb)

    async def _risk_limits(self) -> dict[tuple[str, int], RiskCap]:
        return parse_risk_limits(await self.adapter._request("GET", "/private/account/risk_limit"))

    async def preflight(self, symbols: list[str]) -> None:
        remote = await self.adapter.get_positions()
        if remote:
            raise MexcWebError("canonical preflight refuses to start while a Testnet position is already open")

        unique = sorted({s.upper() for s in symbols if s})
        initial_caps = await self._risk_limits()
        for symbol in unique:
            detail = await self.adapter.get_contract_detail(symbol)
            self._details[symbol] = detail
            contract_max = max(1, int(detail.get("maxLeverage") or 1))
            for ptype in (1, 2):
                prior = initial_caps.get((symbol, ptype))
                risk_max = prior.max_leverage if prior and prior.max_leverage > 0 else contract_max
                leverage = min(self.leverage, contract_max, risk_max)
                if leverage <= 0:
                    self._disabled.add((symbol, ptype))
                    continue
                try:
                    await self.adapter._request(
                        "POST",
                        "/private/position/change_leverage",
                        payload={"openType": 1, "leverage": leverage, "symbol": symbol, "positionType": ptype},
                    )
                except MexcWebError:
                    self._disabled.add((symbol, ptype))
                await asyncio.sleep(0.11)
        self._risk_caps = await self._risk_limits()

    def capacity_base_qty(self, symbol: str, side: OrderSide) -> float:
        key = (symbol.upper(), _position_type(side))
        if key in self._disabled:
            return 0.0
        cap = self._risk_caps.get(key)
        detail = self._details.get(symbol.upper())
        if cap is None or detail is None or cap.max_vol <= 0 or cap.max_leverage <= 0:
            return 0.0
        contract_size = float(detail.get("contractSize") or 0)
        return max(0.0, cap.max_vol * contract_size)

    async def _wait_position(self, symbol: str, side: OrderSide) -> PositionSnapshot | None:
        deadline = time.monotonic() + self.reconcile_timeout_s
        while time.monotonic() < deadline:
            rows = await self.adapter.get_positions(symbol)
            hit = next((p for p in rows if p.side is side and p.qty > 0), None)
            if hit is not None:
                return hit
            await asyncio.sleep(self.poll_s)
        return None

    async def _wait_flat(self, symbol: str) -> bool:
        deadline = time.monotonic() + self.reconcile_timeout_s
        while time.monotonic() < deadline:
            if not await self.adapter.get_positions(symbol):
                return True
            await asyncio.sleep(self.poll_s)
        return False

    async def open_ioc_once(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: float,
        requested_qty: float,
        client_order_id: str,
    ) -> OpenResult:
        symbol = symbol.upper()
        key = (symbol, _position_type(side))
        async with self._lock:
            if self.state is not ExecutionState.FLAT:
                raise MexcWebError(f"execution state is {self.state}, not flat")
            if key in self._disabled:
                return OpenResult(OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_DISABLED", client_order_id), None, ExecutionTiming())
            self.state = ExecutionState.ENTRY_PENDING

        timing = ExecutionTiming()
        try:
            cap_qty = self.capacity_base_qty(symbol, side)
            if cap_qty <= 0:
                self._disabled.add(key)
                async with self._lock:
                    self.state = ExecutionState.FLAT
                return OpenResult(OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_CAPACITY_ZERO", client_order_id), None, timing)
            qty = min(float(requested_qty), cap_qty)
            cap = self._risk_caps[key]
            marks: dict[str, float] = {}
            try:
                fill = await self.adapter.open_ioc(
                    symbol=symbol,
                    side=side,
                    price=price,
                    qty=qty,
                    leverage=min(self.leverage, cap.max_leverage),
                    client_order_id=client_order_id,
                    timing_marks=marks,
                )
            except MexcWebError as exc:
                if "code=8819" in str(exc) or "maximum number of contracts" in str(exc).lower():
                    self._disabled.add(key)
                    fill = OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_8819", client_order_id)
                else:
                    raise
            timing.ioc_post_start_ms = marks.get("ioc_post_start_ms", 0.0)
            timing.ioc_post_response_ms = marks.get("ioc_post_response_ms", 0.0)
            timing.ioc_confirmed_ms = marks.get("ioc_confirmed_ms", 0.0)
            if fill.filled_qty <= 0:
                async with self._lock:
                    self.state = ExecutionState.FLAT
                return OpenResult(fill, None, timing)

            async with self._lock:
                self.state = ExecutionState.RECONCILING
            position = await self._wait_position(symbol, side)
            timing.position_visible_ms = time.time_ns() / 1_000_000.0
            async with self._lock:
                if position is None:
                    self.state = ExecutionState.RECONCILING
                    raise MexcWebError(f"IOC filled but Testnet position was not visible for {symbol}")
                self.position = position
                self.state = ExecutionState.OPEN
            return OpenResult(fill, position, timing)
        except Exception:
            # Resolve ambiguous submit outcomes factually before allowing another entry.
            rows = await self.adapter.get_positions(symbol)
            position = next((p for p in rows if p.side is side and p.qty > 0), None)
            async with self._lock:
                self.position = position
                self.state = ExecutionState.OPEN if position is not None else ExecutionState.FLAT
            raise

    async def close_known_position(self, *, client_order_id: str) -> tuple[OrderFill, ExecutionTiming]:
        async with self._lock:
            if self.state is not ExecutionState.OPEN or self.position is None:
                raise MexcWebError("no canonical OPEN position to close")
            position = self.position
            self.state = ExecutionState.EXIT_PENDING

        timing = ExecutionTiming(close_post_start_ms=time.time_ns() / 1_000_000.0)
        try:
            # Important: no get_position() before submit. The exact known snapshot/positionId is used.
            fill = await self.adapter.close_position_snapshot_reduce_only(position, client_order_id=client_order_id)
            timing.close_confirmed_ms = time.time_ns() / 1_000_000.0
            async with self._lock:
                self.state = ExecutionState.RECONCILING
            if not await self._wait_flat(position.symbol):
                raise MexcWebError(f"close confirmed but position remained visible for {position.symbol}")
            timing.flat_visible_ms = time.time_ns() / 1_000_000.0
            async with self._lock:
                self.position = None
                self.state = ExecutionState.FLAT
            return fill, timing
        except Exception:
            rows = await self.adapter.get_positions(position.symbol)
            remaining = next((p for p in rows if p.position_id == position.position_id), None)
            async with self._lock:
                self.position = remaining
                self.state = ExecutionState.OPEN if remaining is not None else ExecutionState.FLAT
            raise
