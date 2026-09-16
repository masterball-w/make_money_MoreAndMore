"""底层逻辑层 · 风险公式单元测试。"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_layer.formulas import risk


class TestReturnsAndVol:
    def test_simple_returns(self):
        rets = risk.simple_returns([100, 110, 99])
        assert math.isclose(rets[0], 0.10, rel_tol=1e-9)
        assert math.isclose(rets[1], -0.10, rel_tol=1e-9)

    def test_simple_returns_short_input(self):
        assert risk.simple_returns([100]) == []

    def test_zero_vol_constant_series(self):
        assert risk.annualized_volatility([0.01] * 20) == 0.0

    def test_annualized_volatility_positive(self):
        rets = [0.01, -0.02, 0.015, 0.03, -0.01, 0.02]
        vol = risk.annualized_volatility(rets, periods_per_year=252)
        assert vol > 0
        # 与手工计算一致
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        assert math.isclose(vol, math.sqrt(var) * math.sqrt(252), rel_tol=1e-12)


class TestMaxDrawdown:
    def test_basic_drawdown(self):
        curve = [100, 120, 90, 110, 80]
        mdd, peak_i, trough_i = risk.max_drawdown(curve)
        assert math.isclose(mdd, (120 - 80) / 120)
        assert peak_i == 1 and trough_i == 4

    def test_no_drawdown_monotonic_up(self):
        mdd, _, _ = risk.max_drawdown([1, 2, 3, 4])
        assert mdd == 0.0


class TestVaR:
    def test_var_positive_loss(self):
        rets = [-0.05, -0.02, 0.0, 0.01, 0.02, 0.03]
        var = risk.var_historical(rets, confidence=0.95)
        assert var > 0

    def test_var_all_gains(self):
        rets = [0.01, 0.02, 0.03]
        assert risk.var_historical(rets) == 0.0  # 最差也是盈利


class TestSharpe:
    def test_sharpe_zero_when_flat(self):
        assert risk.sharpe_ratio([0.01, 0.01, 0.01]) == 0.0

    def test_sharpe_sign(self):
        good = [0.02, 0.01, 0.03, 0.02]
        bad = [-0.02, -0.01, -0.03, -0.02]
        assert risk.sharpe_ratio(good) > 0 > risk.sharpe_ratio(bad)


class TestKelly:
    def test_even_odds(self):
        # p=0.6, b=1 → f=0.2
        assert math.isclose(risk.kelly_fraction(0.6, 1.0), 0.2)

    def test_no_edge(self):
        assert risk.kelly_fraction(0.5, 1.0) == 0.0

    def test_negative_edge(self):
        assert risk.kelly_fraction(0.3, 1.0) < 0

    def test_invalid_inputs(self):
        assert risk.kelly_fraction(0.0, 1.0) == 0.0
        assert risk.kelly_fraction(0.6, 0.0) == 0.0


class TestPositionSizing:
    def test_risk_based_size(self):
        # 100000 * 1% / (100-95) = 200 股
        assert risk.position_size_by_risk(100_000, 0.01, 100.0, 95.0) == 200.0

    def test_zero_risk_distance(self):
        assert risk.position_size_by_risk(100_000, 0.01, 100.0, 100.0) == 0.0

    def test_stop_loss_price(self):
        assert math.isclose(risk.stop_loss_price(100.0, 0.05), 95.0)
        assert math.isclose(risk.stop_loss_price(100.0, 0.05, is_long=False), 105.0)
