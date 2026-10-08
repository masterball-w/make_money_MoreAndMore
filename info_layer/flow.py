"""资金流源 —— 交易所主动买卖量（taker volume）+ 合约持仓量。

真实源（免费公开，无需 key）：
    OKX     GET /api/v5/rubik/stat/taker-volume?ccy=BTC&instType=SPOT
            → [[ts, sellVol, buyVol], ...] 5分钟粒度
    Binance GET /fapi/v1/openInterest?symbol=BTCUSDT  （可选增强，失败忽略）

Mock 源：平稳随机 + 可注入脉冲（演示"巨鲸大额流入/抛售"行情）。
"""
from __future__ import annotations

import abc
import asyncio
import logging
import random
from datetime import datetime, timezone

import httpx

from common.events import FlowEvent, Market
from common.net import retry_async

logger = logging.getLogger("flow_source")


class FlowProvider(abc.ABC):
    @abc.abstractmethod
    async def next(self, symbol: str) -> FlowEvent: ...


class CryptoFlowSource(FlowProvider):
    """真实资金流源：OKX taker volume（主）+ Binance OI（可选）。"""

    OKX_URL = "https://www.okx.com/api/v5/rubik/stat/taker-volume"
    OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"

    def __init__(self, symbol: str = "BTC/USDT", period: str = "5m", timeout: float = 10.0) -> None:
        self.symbol = symbol
        self.period = period
        self._client = httpx.AsyncClient(timeout=timeout,
                                         headers={"User-Agent": "trading-system/0.1"})
        self._last_ts: str | None = None     # 去重：同一统计周期只发一次

    async def next(self, symbol: str) -> FlowEvent:
        assert symbol == self.symbol
        ccy = symbol.split("/")[0]

        async def _fetch_taker() -> list:
            resp = await self._client.get(self.OKX_URL, params={"ccy": ccy, "instType": "SPOT"})
            resp.raise_for_status()
            return resp.json().get("data", [])

        rows = await retry_async(_fetch_taker, retries=3, backoff=1.5, label="okx-taker")
        if not rows:
            raise ConnectionError("OKX taker-volume 无数据")
        # rows: 新→旧 [ts, sellVol, buyVol]
        latest = rows[0]
        ts, sell_vol, buy_vol = str(latest[0]), float(latest[1]), float(latest[2])
        if ts == self._last_ts:
            raise _NoNewData(f"统计周期 {ts} 未更新")
        self._last_ts = ts

        oi = await self._fetch_oi(symbol)

        await asyncio.sleep(0)
        return FlowEvent(
            symbol=symbol, market=Market.CRYPTO,
            buy_volume=buy_vol, sell_volume=sell_vol,
            net_flow=buy_vol - sell_vol, total_volume=buy_vol + sell_vol,
            period=self.period, open_interest=oi)

    async def _fetch_oi(self, symbol: str) -> float | None:
        """合约持仓量（可选增强：失败静默降级为 None）。"""
        try:
            sym = symbol.replace("/", "")

            async def _get() -> dict:
                resp = await self._client.get(self.OI_URL, params={"symbol": sym})
                resp.raise_for_status()
                return resp.json()

            data = await retry_async(_get, retries=1, backoff=1.0, label="binance-oi")
            return float(data["openInterest"]) if data else None
        except Exception:  # noqa: BLE001
            return None

    async def close(self) -> None:
        await self._client.aclose()


class _NoNewData(Exception):
    """统计周期尚未滚动更新（5m 粒度），调用方应静默跳过本轮。"""


class MockFlowSource(FlowProvider):
    """模拟资金流：平稳随机（买卖大致均衡）+ 可注入大额脉冲。"""

    def __init__(self, symbol: str = "BTC/USDT", period: str = "5m",
                 base_volume: float = 10.0, noise: float = 0.15, seed: int | None = None) -> None:
        self.symbol = symbol
        self.period = period
        self.base = base_volume
        self.noise = noise
        self.rng = random.Random(seed)
        self._pending: FlowEvent | None = None

    async def next(self, symbol: str) -> FlowEvent:
        await asyncio.sleep(0)
        if self._pending is not None:
            ev, self._pending = self._pending, None
            return ev
        total = self.base * self.rng.uniform(1 - self.noise, 1 + self.noise)
        # 平稳期：买卖接近均衡，imbalance 在 ±0.1 内
        imb = self.rng.uniform(-0.1, 0.1)
        buy = total * (1 + imb) / 2
        sell = total * (1 - imb) / 2
        return FlowEvent(symbol=self.symbol, market=Market.CRYPTO,
                         buy_volume=buy, sell_volume=sell,
                         net_flow=buy - sell, total_volume=total, period=self.period)

    def inject_pulse(self, imbalance: float, volume_mult: float = 5.0) -> None:
        """注入大额资金流脉冲：imbalance +0.9 = 巨鲸买入，-0.9 = 大额抛售。"""
        total = self.base * volume_mult
        buy = total * (1 + imbalance) / 2
        sell = total * (1 - imbalance) / 2
        self._pending = FlowEvent(
            symbol=self.symbol, market=Market.CRYPTO,
            buy_volume=buy, sell_volume=sell,
            net_flow=buy - sell, total_volume=total, period=self.period)
