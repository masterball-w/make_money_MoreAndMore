"""底层逻辑层 · 技术指标单元测试（用可手算的已知值验证）。"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_layer import indicators


class TestSMA:
    def test_basic(self):
        out = indicators.sma([1, 2, 3, 4, 5], 3)
        assert out == [None, None, 2.0, 3.0, 4.0]

    def test_period_one(self):
        assert indicators.sma([3.0, 4.0], 1) == [3.0, 4.0]


class TestEMA:
    def test_seed_with_sma(self):
        out = indicators.ema([10, 10, 10, 11], 3)
        assert out[2] == 10.0                      # 种子=前3个SMA
        alpha = 2 / 4
        assert math.isclose(out[3], 11 * alpha + 10 * (1 - alpha))

    def test_short_input(self):
        assert indicators.ema([1.0, 2.0], 5) == [None, None]


class TestRSI:
    def test_all_up(self):
        out = indicators.rsi(list(range(1, 25)), 14)
        assert out[-1] == 100.0                     # 只涨不跌 → RSI=100

    def test_all_down(self):
        out = indicators.rsi(list(range(25, 1, -1)), 14)
        assert out[-1] == 0.0

    def test_range(self):
        prices = [10, 11, 10.5, 12, 11.5, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18]
        out = [v for v in indicators.rsi(prices, 14) if v is not None]
        assert all(0.0 <= v <= 100.0 for v in out)


class TestBollinger:
    def test_band_geometry(self):
        prices = list(range(1, 41))                 # 单调上升
        mid, upper, lower = indicators.bollinger(prices, 20)
        i = 30
        assert mid[i] is not None
        assert lower[i] < mid[i] < upper[i]
        # 对称性
        assert math.isclose(upper[i] - mid[i], mid[i] - lower[i], rel_tol=1e-9)


class TestATR:
    def test_constant_range(self):
        # 价格恒定 → TR=0 → ATR=0
        n = 20
        closes = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n
        out = indicators.atr(highs, lows, closes, 14)
        assert out[-1] is not None and math.isclose(out[-1], 2.0, rel_tol=1e-9)


class TestMACD:
    def test_uptrend_positive_dif(self):
        prices = [float(100 + i) for i in range(60)]
        dif, dea, hist = indicators.macd(prices)
        assert dif[-1] is not None and dif[-1] > 0   # 上升趋势 DIF>0
