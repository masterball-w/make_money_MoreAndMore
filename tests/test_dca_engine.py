"""稳健长期定投引擎单测 —— decide() 纯函数全分支（无 IO，无需 tmp_path）。

覆盖：周定投数学 / 非定投日不买 / SMA200 门禁 / 回调分级取最高档 /
低吸冷却 / 分批止盈 / 跟踪止盈清仓 / 幂等 / 现金上限 / 先卖后买 / 深拷贝。
CFG 数值与 config/risk_limits.yaml 的 dca_engine 段一致。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common.strategy_switch import default_portfolio
from scripts.dca_engine import decide
from scripts.trend_engine import SLIPPAGE

CFG = {"symbol": "SPY", "sma_window": 200, "weekly_amount": 2000, "weekly_day": 0,
       "dip_lookback": 20,
       "dip_tiers": [{"drop": 0.05, "mult": 1}, {"drop": 0.10, "mult": 2},
                     {"drop": 0.15, "mult": 3}],
       "dip_cooldown_days": 5, "tp_trigger": 0.10, "tp_fraction": 0.3333,
       "trail_pct": 0.08}

MON = "2026-11-16"   # 周一（定投日）
TUE = "2026-11-17"
WED = "2026-11-18"


def _pf(cash=100_000.0):
    return default_portfolio(cash)


def _spy(pf, qty, avg, tp_stage=0, trail_high=None):
    pf["positions"]["SPY"] = {
        "qty": qty, "avg_price": avg,
        "trail_high": trail_high if trail_high is not None else avg,
        "tp_stage": tp_stage}
    return pf


class TestWeeklyDca:
    def test_weekly_buy_math(self):
        st, trades = decide(_pf(), 740.0, 700.0, [740.0] * 20, CFG, MON)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        px = 740.0 * (1 + SLIPPAGE)
        qty = round(2000.0 / px, 4)
        assert trades[0]["qty"] == qty and trades[0]["px"] == px
        assert trades[0]["fee"] == 0.0, "美股零佣金"
        assert abs(st["cash"] - (100_000.0 - qty * px)) < 1e-6
        spy = st["positions"]["SPY"]
        assert spy["qty"] == qty and spy["avg_price"] == px and spy["tp_stage"] == 0
        assert st["dca"]["last_dca_date"] == MON

    def test_non_monday_no_buy(self):
        _, trades = decide(_pf(), 740.0, 700.0, [740.0] * 20, CFG, TUE)
        assert trades == []

    def test_sma_gate_blocks_buy(self):
        _, trades = decide(_pf(), 690.0, 700.0, [690.0] * 20, CFG, MON)
        assert trades == [], "收盘低于SMA200暂停周定投（此处无回调故不买）"

    def test_sma_none_blocks_buy(self):
        _, trades = decide(_pf(), 740.0, None, [740.0] * 20, CFG, MON)
        assert trades == [], "基线未建立（SMA 缺失）不买"

    def test_cash_cap(self):
        st, trades = decide(_pf(1000.0), 740.0, 700.0, [740.0] * 20, CFG, MON)
        px = 740.0 * (1 + SLIPPAGE)
        qty = trades[0]["qty"]
        assert qty * px <= 1000.0, "买入金额不得超过可用现金"
        assert abs(qty - 1000.0 / px) < 1e-4
        assert 0 <= st["cash"] < px


class TestDipBuy:
    def test_dip_highest_tier(self):
        closes = [800.0] * 19 + [680.0]     # 距近20日高点回撤 15% → 3 倍
        st, trades = decide(_pf(), 680.0, 600.0, closes, CFG, WED)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        px = 680.0 * (1 + SLIPPAGE)
        assert trades[0]["qty"] == round(6000.0 / px, 4)
        assert "逢低买入" in trades[0]["reason"]
        assert st["dca"]["last_dip_date"] == WED

    def test_dip_cooldown_blocks_same_level(self):
        pf = _pf()
        pf["dca"]["last_dip_date"] = MON
        pf["dca"]["last_dip_drop"] = 0.15      # 上次已低吸到 15% 档
        closes = [800.0] * 19 + [680.0]        # 本次仍 15%，未更深
        _, trades = decide(pf, 680.0, 600.0, closes, CFG, WED)   # 间隔2天 < 5
        assert trades == [], "同档回调且冷却期内不重复低吸"

    def test_deeper_dip_bypasses_cooldown(self):
        pf = _pf()
        pf["dca"]["last_dip_date"] = MON
        pf["dca"]["last_dip_drop"] = 0.10      # 上次 10% 档
        closes = [800.0] * 19 + [680.0]        # 本次跌到 15%，更深
        _, trades = decide(pf, 680.0, 600.0, closes, CFG, WED)   # 间隔2天 < 5
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        assert "3倍" in trades[0]["reason"], "越跌越买：更深回调豁免冷却"

    def test_dip_cooldown_expires(self):
        pf = _pf()
        pf["dca"]["last_dip_date"] = MON
        pf["dca"]["last_dip_drop"] = 0.15      # 同档，靠冷却到期触发
        closes = [800.0] * 19 + [680.0]
        _, trades = decide(pf, 680.0, 600.0, closes, CFG, "2026-11-24")  # 间隔8天
        assert len(trades) == 1 and trades[0]["kind"] == "buy"

    def test_dip_exempt_from_sma_gate(self):
        # 收盘跌破 SMA200（周定投门禁关），但深回调仍允许低吸 —— 崩盘买不到的修复
        closes = [800.0] * 19 + [680.0]
        st, trades = decide(_pf(), 680.0, 700.0, closes, CFG, WED)  # 680 < 700 门禁关
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        assert "豁免门禁" in trades[0]["reason"]
        assert st["dca"]["last_dip_date"] == WED
        assert abs(st["dca"]["last_dip_drop"] - 0.15) < 1e-9

    def test_monday_below_gate_dip_still_buys(self):
        closes = [800.0] * 19 + [680.0]
        _, trades = decide(_pf(), 680.0, 700.0, closes, CFG, MON)  # 周一但门禁关
        assert len(trades) == 1 and "逢低买入" in trades[0]["reason"]


class TestTakeProfit:
    def test_tp_stage1_sells_one_third(self):
        pf = _spy(_pf(), 100.0, 700.0)
        st, trades = decide(pf, 770.0, 700.0, [770.0] * 20, CFG, TUE)
        assert len(trades) == 1 and trades[0]["kind"] == "sell"
        sell_qty = round(100.0 * 0.3333, 4)
        px = 770.0 * (1 - SLIPPAGE)
        assert trades[0]["qty"] == sell_qty and trades[0]["pnl"] > 0
        assert abs(st["cash"] - (100_000.0 + sell_qty * px)) < 1e-6
        spy = st["positions"]["SPY"]
        assert spy["qty"] == round(100.0 - sell_qty, 8)
        assert spy["tp_stage"] == 1 and spy["trail_high"] == 770.0

    def test_tp_ignores_gate(self):
        pf = _spy(_pf(), 100.0, 700.0)
        _, trades = decide(pf, 770.0, 800.0, [770.0] * 20, CFG, TUE)
        assert len(trades) == 1 and trades[0]["kind"] == "sell", "门禁只拦买入，不拦止盈"

    def test_trail_stop_liquidates(self):
        pf = _spy(_pf(), 66.67, 700.0, tp_stage=1, trail_high=800.0)
        st, trades = decide(pf, 730.0, 700.0, [730.0] * 20, CFG, TUE)
        assert len(trades) == 1 and trades[0]["kind"] == "sell"
        assert trades[0]["qty"] == 66.67, "回撤超8%清仓"
        assert "SPY" not in st["positions"]
        px = 730.0 * (1 - SLIPPAGE)
        assert abs(st["cash"] - (100_000.0 + 66.67 * px)) < 1e-6

    def test_trail_high_ratchets_up(self):
        pf = _spy(_pf(), 66.67, 700.0, tp_stage=1, trail_high=780.0)
        st, trades = decide(pf, 790.0, 700.0, [790.0] * 20, CFG, TUE)
        assert trades == [], "未回撤8%不动作"
        assert st["positions"]["SPY"]["trail_high"] == 790.0


class TestDiscipline:
    def test_same_candle_idempotent(self):
        st, t1 = decide(_pf(), 740.0, 700.0, [740.0] * 20, CFG, MON)
        assert len(t1) == 1
        cash, qty = st["cash"], st["positions"]["SPY"]["qty"]
        st2, t2 = decide(st, 999.0, 100.0, [999.0] * 20, CFG, MON)
        assert t2 == [], "同一根日线重复运行不得产生交易"
        assert st2["cash"] == cash and st2["positions"]["SPY"]["qty"] == qty
        assert st2["dca"]["last_price"] == 999.0, "展示字段允许刷新"

    def test_sell_before_buy(self):
        pf = _spy(_pf(), 100.0, 700.0)
        st, trades = decide(pf, 770.0, 700.0, [770.0] * 20, CFG, MON)
        assert [t["kind"] for t in trades] == ["sell", "buy"], "先卖后买"
        px_sell = 770.0 * (1 - SLIPPAGE)
        px_buy = 770.0 * (1 + SLIPPAGE)
        cash_after_sell = 100_000.0 + round(100.0 * 0.3333, 4) * px_sell
        buy_qty = round(2000.0 / px_buy, 4)
        assert trades[1]["qty"] == buy_qty
        assert abs(st["cash"] - (cash_after_sell - buy_qty * px_buy)) < 1e-6

    def test_deepcopy_input_unchanged(self):
        pf = _spy(_pf(), 100.0, 700.0)
        before = copy.deepcopy(pf)
        decide(pf, 770.0, 700.0, [770.0] * 20, CFG, MON)
        assert pf == before, "decide 不得修改入参"
