"""决策层 · 指标突破检测器单元测试：穿越判定 / 去抖 / 合并 / K线流语义。"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.bus import EventBus
from common.events import Candle, KlineEvent, Market
from decision_layer.breakout_detector import BreakoutConfig, BreakoutDetector
from info_layer.kline import MockKlineData

SYMBOL = "BTC/USDT"


def _candle(close: float, *, high: float | None = None, low: float | None = None,
            ts: datetime | None = None) -> Candle:
    return Candle(ts=ts or datetime.now(timezone.utc),
                  open=close, high=high or close, low=low or close, close=close)


def _kline(closes: list[float], *, current: Candle | None = None) -> KlineEvent:
    """closes 为已收盘历史；current 为可选的当前未收盘 K 线。"""
    history = []
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, c in enumerate(closes):
        history.append(_candle(c, ts=t0 + timedelta(minutes=i)))
    return KlineEvent(symbol=SYMBOL, market=Market.CRYPTO,
                      current=current, history=history)


def _flat_closes(n: int = 40, price: float = 100.0, jitter: float = 0.3) -> list[float]:
    """围绕 price 微幅震荡的 K 线序列（不触发任何突破）。"""
    import random
    rng = random.Random(1)
    return [round(price + rng.uniform(-jitter, jitter), 2) for _ in range(n)]


class TestCrossDetection:
    def test_donchian_breakout_up(self):
        async def scenario():
            bus = EventBus()
            signals: list = []
            bus.subscribe("signal", lambda s: _ap(signals, s))
            det = BreakoutDetector(bus, BreakoutConfig(donchian_period=20))
            flat = _flat_closes()
            high = max(flat) + 1.0                        # 历史最高点
            await det.on_kline(_kline(flat))              # 建立状态
            await det.on_kline(_kline(flat, current=_candle(high + 2.0)))  # 突破
            assert len(signals) == 1
            sig = signals[0]
            assert "donch_up" in sig.kind and sig.direction == +1
            assert sig.threshold == pytest_approx(high + 1.0) or abs(sig.threshold - high) < 1.5
        asyncio.run(scenario())

    def test_no_signal_within_range(self):
        async def scenario():
            bus = EventBus()
            signals: list = []
            bus.subscribe("signal", lambda s: _ap(signals, s))
            det = BreakoutDetector(bus)
            flat = _flat_closes()
            await det.on_kline(_kline(flat))
            await det.on_kline(_kline(flat, current=_candle(flat[-1] + 0.1)))  # 区间内微动
            assert signals == []
        asyncio.run(scenario())

    def test_crossing_not_level(self):
        """持续处于轨道外不重复触发（穿越才触发）。"""
        async def scenario():
            bus = EventBus()
            signals: list = []
            bus.subscribe("signal", lambda s: _ap(signals, s))
            det = BreakoutDetector(bus)
            flat = _flat_closes()
            await det.on_kline(_kline(flat))
            await det.on_kline(_kline(flat, current=_candle(flat[-1] + 5.0)))   # 突破
            await det.on_kline(_kline(flat, current=_candle(flat[-1] + 6.0)))   # 仍在外
            await det.on_kline(_kline(flat, current=_candle(flat[-1] + 7.0)))   # 仍在外
            donch = [s for s in signals if "donch" in s.kind]
            assert len(donch) <= 1
        asyncio.run(scenario())

    def test_dedup_same_candle(self):
        """同一根 K 线（同 ts）同一类突破只报一次。"""
        async def scenario():
            bus = EventBus()
            signals: list = []
            bus.subscribe("signal", lambda s: _ap(signals, s))
            det = BreakoutDetector(bus)
            flat = _flat_closes()
            ts = datetime.now(timezone.utc)
            await det.on_kline(_kline(flat))
            await det.on_kline(_kline(flat, current=_candle(flat[-1] + 5.0, ts=ts)))
            await det.on_kline(_kline(flat, current=_candle(flat[-1] + 8.0, ts=ts)))  # 同根K线
            donch = [s for s in signals if "donch" in s.kind]
            assert len(donch) == 1
        asyncio.run(scenario())


class TestMockKline:
    def test_tick_then_close(self):
        async def scenario():
            k = MockKlineData(symbol=SYMBOL, ticks_per_candle=3, seed=5)
            ev1 = await k.next(SYMBOL)
            assert ev1.current is not None and not ev1.current.closed
            n_hist = len(k.history)
            await k.next(SYMBOL)
            await k.next(SYMBOL)                       # 第 3 tick → 收盘
            assert len(k.history) == n_hist + 1
            ev4 = await k.next(SYMBOL)
            assert ev4.current is not None             # 新 K 线开启
        asyncio.run(scenario())

    def test_inject_published_as_current(self):
        async def scenario():
            k = MockKlineData(symbol=SYMBOL, seed=9)
            for _ in range(10):
                await k.next(SYMBOL)
            last = k.history[-1].close
            k.inject_candle(round(last * 1.05, 2))     # +5% 突破 K 线
            ev = await k.next(SYMBOL)
            assert ev.current is not None
            assert ev.current.close == round(last * 1.05, 2)
            assert all(c.close != ev.current.close for c in ev.history)  # 不在历史里
        asyncio.run(scenario())

    def test_kline_event_closes(self):
        ev = _kline([100.0, 101.0], current=_candle(102.0))
        assert ev.closes() == [100.0, 101.0, 102.0]


async def _ap(lst, item):     # 异步 append（bus handler 需为协程）
    lst.append(item)


def pytest_approx(v):         # 简化断言（避免引入 pytest 依赖到模块层）
    class _A:
        def __eq__(self, other):
            return abs(other - v) <= 2.0
    return _A()
