"""真实 K 线源 —— 交易所公开 REST API（行情数据无需 API key）。

支持 OKX（主力，本机延迟最低）与 Binance（备用），自动故障切换：
    OKX     GET /api/v5/market/candles?instId=BTC-USDT&bar=1m
    Binance GET /api/v3/klines?symbol=BTCUSDT&interval=1m

公开行情接口有限速（OKX 20次/2s，Binance 1200权重/分钟），轮询间隔
由 aggregator 的 kline_poll_interval 控制（1m K 线建议 5~10 秒）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from common.events import Candle, KlineEvent, Market

logger = logging.getLogger("rest_kline")


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class _OKX:
    name = "okx"
    BASE = "https://www.okx.com"
    _BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}

    def __init__(self, symbol: str, interval: str) -> None:
        self.symbol = symbol
        self.interval = interval

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[Candle]:
        bar = self._BAR.get(self.interval, "1m")
        inst = self.symbol.replace("/", "-")
        resp = await client.get(f"{self.BASE}/api/v5/market/candles",
                                params={"instId": inst, "bar": bar, "limit": min(limit, 300)})
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        # OKX 返回 新→旧，字段 [ts,o,h,l,c,vol,...,confirm]
        candles = [
            Candle(ts=_ms_to_utc(int(r[0])), open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5]),
                   closed=str(r[8]) == "1")
            for r in rows
        ]
        return list(reversed(candles))          # 旧 → 新


class _Binance:
    name = "binance"
    BASE = "https://api.binance.com"

    def __init__(self, symbol: str, interval: str) -> None:
        self.symbol = symbol
        self.interval = interval

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[Candle]:
        sym = self.symbol.replace("/", "")
        resp = await client.get(f"{self.BASE}/api/v3/klines",
                                params={"symbol": sym, "interval": self.interval,
                                        "limit": min(limit, 1000)})
        resp.raise_for_status()
        rows = resp.json()
        # Binance 返回 旧→新 [openTime,o,h,l,c,v,closeTime,...]，最后一根未收盘
        candles = [
            Candle(ts=_ms_to_utc(int(r[0])), open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in rows
        ]
        if candles:
            candles[-1].closed = False
        return candles


class RESTKlineSource:
    """真实 K 线源：主备切换 + 指数退避重试。

    next() 语义与 MockKlineData 一致：返回当前未收盘 K 线 + 已收盘历史。
    """

    def __init__(self, symbol: str = "BTC/USDT", interval: str = "1m",
                 exchanges: list[str] | None = None, limit: int = 150,
                 timeout: float = 10.0) -> None:
        self.symbol = symbol
        self.interval = interval
        self.limit = limit
        makers: dict[str, type] = {"okx": _OKX, "binance": _Binance}
        names = exchanges or ["okx", "binance"]
        self._apis = [makers[n](symbol, interval) for n in names]
        self._active = 0
        self._fail_streak = 0
        self._client = httpx.AsyncClient(timeout=timeout,
                                         headers={"User-Agent": "trading-system/0.1"})

    async def next(self, symbol: str) -> KlineEvent:
        assert symbol == self.symbol
        candles = await self._fetch_with_failover()
        current = None
        history = candles
        if history and not history[-1].closed:
            current = history.pop()
        return KlineEvent(symbol=self.symbol, market=Market.CRYPTO,
                          interval=self.interval, current=current, history=history)

    async def _fetch_with_failover(self) -> list[Candle]:
        errors: list[str] = []
        n = len(self._apis)
        for i in range(n):
            api = self._apis[(self._active + i) % n]
            try:
                # 指数退避：连续失败越多等越久（封顶 30s），避免打爆限速
                if self._fail_streak:
                    await asyncio.sleep(min(2.0 ** min(self._fail_streak, 5), 30.0))
                candles = await api.fetch(self._client, self.limit)
                self._active = (self._active + i) % n
                self._fail_streak = 0
                return candles
            except Exception as e:  # noqa: BLE001
                self._fail_streak += 1
                errors.append(f"{api.name}: {type(e).__name__} {e}")
                logger.warning("K线源 %s 失败（连续%d次），切换备用源",
                               api.name, self._fail_streak)
        raise ConnectionError("所有K线源均失败: " + "; ".join(errors))

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def active_exchange(self) -> str:
        return self._apis[self._active].name
