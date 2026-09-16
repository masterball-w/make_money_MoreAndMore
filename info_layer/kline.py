"""实时 K 线源 —— OHLCV 蜡烛图数据（信息层行情主力）。

- MockKlineData: 随机游走生成 K 线流（演示/回测），支持注入自定义 K 线构造突破行情
- CCXTKlineSource: 交易所实时 K 线（阶段2，ccxt 可选依赖）

K 线语义：history 全部已收盘；current 是正在形成的一根（closed=False），
每个 tick 更新其 close/high/low，收盘后落入 history 并开新根。
"""
from __future__ import annotations

import abc
import asyncio
import random
from datetime import datetime, timedelta, timezone

from common.events import Candle, KlineEvent, Market


class KlineProvider(abc.ABC):
    """K 线源接口：每次调用返回一次 K 线流更新。"""

    @abc.abstractmethod
    async def next(self, symbol: str) -> KlineEvent: ...


class MockKlineData(KlineProvider):
    """几何随机游走的 OHLCV K 线生成器。

    ticks_per_candle: 每根 K 线被"实时更新"多少次（模拟盘中 tick 流）。
    inject(): 允许外部直接注入自定义 K 线（测试/演示构造突破行情）。
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        start_price: float = 50_000.0,
        interval: str = "1m",
        drift: float = 0.0002,
        volatility: float = 0.002,
        history_len: int = 60,
        ticks_per_candle: int = 4,
        seed: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.interval = interval
        self.drift = drift
        self.volatility = volatility
        self.ticks_per_candle = ticks_per_candle
        self.rng = random.Random(seed)
        self._tick_in_candle = 0
        self._pending: Candle | None = None       # 预约注入的突破 K 线
        self._now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._now -= timedelta(minutes=history_len)
        self.history: list[Candle] = []
        self.current: Candle | None = None
        # 预生成历史 K 线，让指标立刻可用
        p = start_price / (1 + drift) ** history_len
        for _ in range(history_len):
            self.history.append(self._next_candle(p))
            p = self.history[-1].close

    # ---------------------------------------------------------------- 生成

    def _next_candle(self, prev_close: float) -> Candle:
        o = prev_close
        c = o * (1 + self.rng.gauss(self.drift, self.volatility))
        hi = max(o, c) * (1 + abs(self.rng.gauss(0, self.volatility * 0.4)))
        lo = min(o, c) * (1 - abs(self.rng.gauss(0, self.volatility * 0.4)))
        return Candle(
            ts=self._now, open=round(o, 2), high=round(hi, 2),
            low=round(lo, 2), close=round(c, 2),
            volume=round(self.rng.uniform(50, 500), 2), closed=True)

    async def next(self, symbol: str) -> KlineEvent:
        assert symbol == self.symbol
        # 优先发布预约注入的 K 线（作为当前 K 线，让突破检测与历史比较）
        if self._pending is not None:
            injected = self._pending
            self._pending = None
            injected.closed = True
            self.history.append(injected)
            self.current = None
            self._tick_in_candle = 0
            self._now += timedelta(seconds=15)
            await asyncio.sleep(0)
            # 发布时注入 K 线既在 current 位又在 history（detector 以 current 语义解读）
            return KlineEvent(symbol=self.symbol, market=Market.CRYPTO,
                              interval=self.interval,
                              current=Candle(ts=injected.ts, open=injected.open,
                                             high=injected.high, low=injected.low,
                                             close=injected.close,
                                             volume=injected.volume, closed=False),
                              history=list(self.history[:-1][-150:]))
        self._tick_in_candle += 1
        if self.current is None:
            self.current = self._open_new()
        else:
            # 盘中 tick：只更新当前 K 线的 close/high/low（实时 K 线图语义）
            last = self.history[-1].close if self.history else self.current.open
            new_close = round(
                self.current.close * (1 + self.rng.gauss(self.drift / self.ticks_per_candle,
                                                         self.volatility)), 2)
            self.current.close = new_close
            self.current.high = max(self.current.high, new_close)
            self.current.low = min(self.current.low, new_close)
            self.current.volume += self.rng.uniform(5, 50)
        if self._tick_in_candle >= self.ticks_per_candle:
            # 当前 K 线收盘 → 落入 history，下一轮开新根
            self.current.closed = True
            self.history.append(self.current)
            self.current = None
            self._tick_in_candle = 0
        self._now += timedelta(seconds=15)   # 模拟时间推进
        await asyncio.sleep(0)
        return KlineEvent(
            symbol=self.symbol, market=Market.CRYPTO, interval=self.interval,
            current=self.current, history=list(self.history[-150:]))

    def _open_new(self) -> Candle:
        base = self.history[-1].close if self.history else 50_000.0
        return Candle(ts=self._now, open=base, high=base, low=base,
                      close=base, volume=0.0, closed=False)

    # ---------------------------------------------------------------- 注入

    def inject_candle(self, close: float, *, high: float | None = None,
                      low: float | None = None, volume: float = 100.0) -> None:
        """预约注入一根自定义 K 线（演示/测试构造突破行情）。

        下一次 next() 将其作为"当前 K 线"发布（closed=False 语义），
        使突破检测器把它与更早的历史比较 —— 正是"突破前 N 根高点"的语义。
        """
        base = (self.current.close if self.current else
                self.history[-1].close if self.history else 50_000.0)
        high = high if high is not None else max(base, close) * 1.001
        low = low if low is not None else min(base, close) * 0.999
        self._pending = Candle(
            ts=self._now, open=base, high=round(high, 2), low=round(low, 2),
            close=round(close, 2), volume=volume, closed=False)


class CCXTKlineSource(KlineProvider):
    """交易所实时 K 线（阶段2实装，ccxt 可选依赖）。"""

    def __init__(self, exchange_id: str = "binance", interval: str = "1m") -> None:
        self.exchange_id = exchange_id
        self.interval = interval
        self._seen: set[str] = set()

    async def next(self, symbol: str) -> KlineEvent:
        try:
            import ccxt.async_support as ccxt  # type: ignore[import-untyped]
        except ImportError as e:
            raise NotImplementedError("pip install ccxt 后在阶段2启用实时 K 线") from e
        ex = getattr(ccxt, self.exchange_id)()
        try:
            raw = await ex.fetch_ohlcv(symbol, timeframe=self.interval, limit=150)
        finally:
            await ex.close()
        candles = [
            Candle(ts=datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc),
                   open=float(c[1]), high=float(c[2]), low=float(c[3]),
                   close=float(c[4]), volume=float(c[5]))
            for c in raw
        ]
        current = candles.pop() if candles else None
        if current is not None:
            current.closed = False
        return KlineEvent(symbol=symbol, market=Market.CRYPTO,
                          interval=self.interval, current=current, history=candles)
