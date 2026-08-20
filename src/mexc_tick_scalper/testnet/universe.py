from __future__ import annotations

import time

from ..lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from ..live_zero_fee_universe import LIVE_REST, LIVE_WS, LiveZeroFeeContract
from ..market import MexcPublicMarket
from ..web_execution import MexcWebExecutionAdapter, WebExecutionConfig
from ..web_fee import read_web_fee_provider
from .models import FeeScope, PublicContract, TestnetContract


class TestnetUniverseService:
    """Build the Binance/MEXC/Testnet research universe.

    This service receives an already-built Demo read-only config. It never loads
    environment variables and never constructs LIVE private authentication.
    """

    def __init__(self, console, execution_config: WebExecutionConfig) -> None:
        self.console = console
        self.execution_config = execution_config

    @staticmethod
    def _contract_rows(payload) -> list[dict]:
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            if "symbol" in data:
                return [data]
            return [row for row in data.values() if isinstance(row, dict)]
        return []

    async def _public_crosslisted(self) -> list[PublicContract]:
        self.console.print(
            "[cyan][1/4][/cyan] Loading public Binance USD-M + LIVE MEXC contracts; no LIVE web token is used."
        )
        binance_symbols = await fetch_binance_usdm_symbols()
        mexc_rows = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()

        rows: list[PublicContract] = []
        for raw in mexc_rows:
            symbol = str(raw.get("symbol") or "").upper()
            if not symbol:
                continue
            binance_symbol = mexc_to_binance_symbol(symbol)
            if binance_symbol not in binance_symbols:
                continue
            contract_size = float(raw.get("contractSize") or 0)
            if contract_size <= 0:
                continue
            rows.append(
                PublicContract(
                    LiveZeroFeeContract(
                        mexc_symbol=symbol,
                        binance_symbol=binance_symbol,
                        max_leverage=int(raw.get("maxLeverage") or 1),
                        contract_size=contract_size,
                        min_vol=float(raw.get("minVol") or 0),
                        maintenance_margin_rate=float(raw.get("maintenanceMarginRate") or 0),
                        initial_margin_rate=float(raw.get("initialMarginRate") or 0),
                        risk_base_vol=float(raw.get("riskBaseVol") or 0),
                        risk_incr_vol=float(raw.get("riskIncrVol") or 0),
                        risk_incr_mmr=float(raw.get("riskIncrMmr") or 0),
                        risk_level_limit=max(1, int(raw.get("riskLevelLimit") or 1)),
                        risk_limit_type=str(raw.get("riskLimitType") or "BY_VOLUME").upper(),
                    )
                )
            )
        rows.sort(key=lambda row: row.symbol)
        self.console.print(f"[cyan][1/4][/cyan] Cross-listed public contracts={len(rows)}")
        return rows

    async def _demo_metadata(self) -> tuple[dict[str, dict], object, float]:
        async with MexcWebExecutionAdapter(self.execution_config) as adapter:
            started = time.perf_counter_ns()
            payload = await adapter._request("GET", "/contract/detail")
            rtt_ms = (time.perf_counter_ns() - started) / 1_000_000.0
            fee_provider = await read_web_fee_provider(adapter)

        details: dict[str, dict] = {}
        for row in self._contract_rows(payload):
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                details[symbol] = row
        return details, fee_provider, rtt_ms

    async def load(self, fee_scope: FeeScope) -> list[TestnetContract]:
        public_rows = await self._public_crosslisted()
        if not public_rows:
            raise RuntimeError("No Binance/MEXC cross-listed Futures contracts were found")

        self.console.print(
            f"[cyan][2/4][/cyan] Loading Testnet contract metadata + Demo fee rates for {len(public_rows)} candidates."
        )
        details, fee_provider, rtt_ms = await self._demo_metadata()

        out: list[TestnetContract] = []
        zero_count = 0
        unknown_count = 0
        for public in public_rows:
            detail = details.get(public.symbol)
            if detail is None:
                continue
            if float(detail.get("contractSize") or 0) <= 0 or float(detail.get("priceUnit") or 0) <= 0:
                continue
            max_lev = int(detail.get("maxLeverage") or 0)
            if max_lev <= 0:
                continue

            fee = fee_provider.status(public.symbol)
            maker, taker = fee.maker, fee.taker
            exact_zero = maker == 0.0 and taker == 0.0
            zero_count += int(exact_zero)
            unknown_count += int(maker is None or taker is None)
            if fee_scope is FeeScope.ZERO_ONLY and not exact_zero:
                continue

            out.append(
                TestnetContract(
                    contract=public.contract,
                    demo_maker_fee=maker,
                    demo_taker_fee=taker,
                    demo_max_leverage=max_lev,
                    metadata_rtt_ms=rtt_ms,
                )
            )

        scope_text = "all fees" if fee_scope is FeeScope.ALL else "explicit Demo 0/0 only"
        self.console.print(
            f"[cyan][2/4][/cyan] Testnet contracts={len(details)}; Demo 0/0={zero_count}; "
            f"fee_unknown={unknown_count}; usable={len(out)}; scope={scope_text}; metadata RTT={rtt_ms:.0f}ms"
        )
        return out
