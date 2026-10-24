"""美股趋势引擎单测 —— decide() 纯函数全分支 + 美股市场规则 + SMA 计算。

decide() 是引擎唯一的决策入口（无 IO），这里覆盖：
基线建立 / 幂等跳过 / 进场买入的数学 / 清仓卖出的数学 / 空仓观望不动作。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common.events import Market
from knowledge_layer.market_rules import MarketRuleBook
from scripts.trend_engine import SLIPPAGE, compute_sma, decide

CFG = {"sma_window": 200, "exposure": 0.5}


def _fresh_state(cash=100_000.0):
    return {"cash": cash, "qty": 0.0, "avg_price": 0.0,
            "last_signal": None, "last_candle": "", "bootstrapped": False}


class TestComputeSma:
    def test_exact_window(self):
        vals = [1.0] * 200
        assert compute_sma(vals, 200) == 1.0

    def test_weighted(self):
        # 3 根窗口: [2,4,6] → 均值 4
        assert compute_sma([2.0, 4.0, 6.0], 3) == 4.0

    def test_insufficient_returns_none(self):
        assert compute_sma([1.0, 2.0], 200) is None


class TestDecide:
    def test_bootstrap_records_baseline_no_trade(self):
        st, trade = decide(_fresh_state(), price=500.0, sma=400.0,
                           cfg=CFG, candle_date="2026-10-24")
        assert trade is None, "首日只建基线不交易"
        assert st["bootstrapped"] is True
        assert st["last_signal"] is True          # 500 > 400
        assert st["last_candle"] == "2026-10-24"
        assert st["cash"] == 100_000.0 and st["qty"] == 0.0

    def test_same_candle_idempotent(self):
        st = _fresh_state()
        st, _ = decide(st, 500.0, 400.0, CFG, "2026-10-24")
        before = dict(st)
        st2, trade = decide(st, 999.0, 100.0, CFG, "2026-10-24")   # 完全不同的行情
        assert trade is None and st2 == before, "同一根日线重复运行必须零动作"

    def test_buy_math_half_exposure(self):
        st = _fresh_state()
        st, _ = decide(st, 500.0, 400.0, CFG, "2026-10-23")        # 基线：信号=持有
        st, trade = decide(st, 500.0, 400.0, CFG, "2026-10-24")   # 信号开着 → 进场
        assert trade is not None and trade["kind"] == "buy"
        px = 500.0 * (1 + SLIPPAGE)
        expected_qty = round(100_000.0 * 0.5 / px, 4)
        assert abs(trade["qty"] - expected_qty) < 1e-9
        assert trade["px"] == px
        assert trade["fee"] == 0.0, "美股零佣金"
        assert abs(st["cash"] - 50_000.0) < 1e-6
        assert st["qty"] == expected_qty
        assert st["last_signal"] is True

    def test_sell_math_and_pnl(self):
        st = _fresh_state()
        st, _ = decide(st, 500.0, 400.0, CFG, "2026-10-23")
        st, _ = decide(st, 500.0, 400.0, CFG, "2026-10-24")       # 买入
        qty_before = st["qty"]
        cash_before = st["cash"]
        st, trade = decide(st, 380.0, 400.0, CFG, "2026-10-25")   # 跌破均线 → 清仓
        assert trade is not None and trade["kind"] == "sell"
        px = 380.0 * (1 - SLIPPAGE)
        assert trade["qty"] == qty_before
        assert trade["pnl"] < 0, "380 买入均价≈500，本轮必亏"
        assert st["qty"] == 0.0 and st["avg_price"] == 0.0
        assert abs(st["cash"] - (cash_before + qty_before * px)) < 1e-6
        assert st["last_signal"] is False

    def test_flat_and_signal_off_does_nothing(self):
        st = _fresh_state()
        st, _ = decide(st, 300.0, 400.0, CFG, "2026-10-23")        # 基线：信号=观望
        st, trade = decide(st, 300.0, 400.0, CFG, "2026-10-24")
        assert trade is None, "空仓且信号仍为观望，不动作"
        assert st["cash"] == 100_000.0 and st["qty"] == 0.0

    def test_no_buy_when_signal_flips_off(self):
        # 基线时信号=持有但尚未建仓；次日信号翻空 → 仍然不买
        st = _fresh_state()
        st, _ = decide(st, 500.0, 400.0, CFG, "2026-10-23")
        st, trade = decide(st, 380.0, 400.0, CFG, "2026-10-24")
        assert trade is None, "空仓+信号翻空，不动作"


class TestUSMarketRules:
    def test_us_stock_rule_registered(self):
        rule = MarketRuleBook().rule(Market.US_STOCK)
        assert rule.fee_rate == 0.0
        assert rule.min_qty_step == 0.01
        assert rule.t_plus == 0

    def test_round_qty_fractional(self):
        book = MarketRuleBook()
        assert book.round_qty(Market.US_STOCK, 65.43219) == 65.43

    def test_enum_values(self):
        assert Market.US_STOCK.value == "us_stock"
