"""行情适配器 —— 统一 MarketDataProvider 接口。

- MockMarketData: 随机游走生成价格（演示/测试）
- CCXTMarketData:  加密实时行情（阶段2，ccxt 可选依赖）
- AKShareMarketData: A股行情（阶段2，akshare 可选依赖）
"""
from __future__ import annotations

import abc
import asyncio
import random

from common.events import Market, MarketEvent


class MarketDataProvider(abc.ABC):
    @abc.abstractmethod
    async def snapshot(self, symbol: str) -> MarketEvent:
        """返回一次行情快照（含近期收盘价序列，供指标计算）。"""


class MockMarketData(MarketDataProvider):
    """几何随机游走：演示链路时提供可控的"行情"。"""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        start_price: float = 50_000.0,
        drift: float = 0.0008,
        volatility: float = 0.004,
        history_len: int = 60,
        seed: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.drift = drift
        self.volatility = volatility
        self.rng = random.Random(seed)
        # 预生成一段历史，使指标立刻有值
        p = start_price / (1 + drift) ** history_len
        self.history: list[float] = []
        for _ in range(history_len):
            p *= 1 + self.rng.gauss(drift, volatility)
            self.history.append(round(p, 2))

    async def snapshot(self, symbol: str) -> MarketEvent:
        assert symbol == self.symbol, f"MockMarketData 只服务 {self.symbol}"
        last = self.history[-1]
        new_price = round(last * (1 + self.rng.gauss(self.drift, self.volatility)), 2)
        self.history.append(new_price)
        change_pct = (new_price - last) / last if last else 0.0
        await asyncio.sleep(0)
        return MarketEvent(
            symbol=symbol,
            market=Market.CRYPTO,
            price=new_price,
            change_pct=change_pct,
            volume=float(self.rng.uniform(100, 1000)),
            history=list(self.history[-120:]),
        )


class CCXTMarketData(MarketDataProvider):
    """加密实时行情（阶段2实装）。当前仅提供接口与占位实现。"""

    def __init__(self, exchange_id: str = "binance") -> None:
        self.exchange_id = exchange_id

    async def snapshot(self, symbol: str) -> MarketEvent:
        try:
            import ccxt.async_support as ccxt  # type: ignore[import-untyped]
        except ImportError as e:
            raise NotImplementedError("pip install ccxt 后在阶段2启用实时行情") from e
        ex = getattr(ccxt, self.exchange_id)()
        try:
            ticker = await ex.fetch_ticker(symbol)
            ohlcv = await ex.fetch_ohlcv(symbol, timeframe="1h", limit=120)
            return MarketEvent(
                symbol=symbol,
                market=Market.CRYPTO,
                price=float(ticker["last"]),
                change_pct=float(ticker.get("percentage") or 0.0),
                volume=float(ticker.get("quoteVolume") or 0.0),
                history=[float(c[4]) for c in ohlcv],
            )
        finally:
            await ex.close()
