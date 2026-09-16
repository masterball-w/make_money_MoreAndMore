"""决策层 · 风控门禁单元测试：R1 削减 / R3 熔断 / R4 事件窗口 / SELL 宽松。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.events import Action, DecisionEvent, Market
from common.models import Account
from decision_layer.risk_manager import RiskLimits, RiskManager


def _buy(qty: float, symbol: str = "BTC/USDT", price: float = 100.0) -> DecisionEvent:
    return DecisionEvent(action=Action.BUY, symbol=symbol, market=Market.CRYPTO,
                         quantity=qty, reference_price=price)


def _sell(qty: float, symbol: str = "BTC/USDT", price: float = 100.0) -> DecisionEvent:
    return DecisionEvent(action=Action.SELL, symbol=symbol, market=Market.CRYPTO,
                         quantity=qty, reference_price=price)


class TestR1PerTradeRisk:
    def test_oversized_buy_trimmed(self):
        account = Account(cash=100_000)
        rm = RiskManager(account)
        d = rm.check(_buy(qty=50, price=100.0), price=100.0, calendar_window=False)
        # 净值10万 * 1% = 1000 → 最多 10 个单位
        assert d.quantity == 10.0
        assert not d.blocked_by_risk

    def test_small_buy_passes(self):
        rm = RiskManager(Account(cash=100_000))
        d = rm.check(_buy(qty=5), price=100.0, calendar_window=False)
        assert d.quantity == 5.0


class TestR2PositionCap:
    def test_position_full_rejects(self):
        account = Account(cash=100_000)
        pos = account.position("BTC/USDT", Market.CRYPTO)
        pos.qty, pos.avg_price = 250.0, 100.0           # 市值 25000 > 20% 上限
        rm = RiskManager(account)
        d = rm.check(_buy(qty=1), price=100.0, calendar_window=False)
        assert d.blocked_by_risk and d.quantity == 0


class TestR3Halt:
    def test_halted_blocks_buy(self):
        account = Account(cash=100_000, halted=True)
        rm = RiskManager(account)
        d = rm.check(_buy(qty=1), price=100.0, calendar_window=False)
        assert d.blocked_by_risk

    def test_drawdown_triggers_halt(self):
        account = Account(cash=0)
        account.high_water_mark = 100_000
        rm = RiskManager(account, RiskLimits(max_drawdown_halt=0.10))
        # 持仓贬值后净值 89000 → 回撤 11% → 熔断
        pos = account.position("BTC/USDT", Market.CRYPTO)
        pos.qty, pos.avg_price = 1.0, 100_000.0
        dd = rm.update_drawdown({"BTC/USDT": 89_000.0})
        assert dd >= 0.10 and account.halted
        rm.unlock()
        assert not account.halted


class TestR4EventWindow:
    def test_event_window_halves_size(self):
        account = Account(cash=100_000)
        rm = RiskManager(account, RiskLimits(event_window_scale=0.5))
        d = rm.check(_buy(qty=4), price=100.0, calendar_window=True)
        assert d.quantity == 2.0


class TestR5Cooldown:
    def test_cooldown_blocks_reentry(self):
        rm = RiskManager(Account(cash=100_000), RiskLimits(cooldown_minutes=10))
        rm.record_trade("BTC/USDT")
        d = rm.check(_buy(qty=1), price=100.0, calendar_window=False)
        assert d.blocked_by_risk


class TestSellLeniency:
    def test_sell_not_blocked_when_halted(self):
        account = Account(cash=0, halted=True)
        pos = account.position("BTC/USDT", Market.CRYPTO)
        pos.qty, pos.avg_price = 2.0, 100.0
        rm = RiskManager(account)
        d = rm.check(_sell(qty=2), price=90.0, calendar_window=False)
        assert d.quantity == 2.0 and not d.blocked_by_risk   # 降风险方向永远放行

    def test_sell_trimmed_to_position(self):
        account = Account(cash=0)
        pos = account.position("BTC/USDT", Market.CRYPTO)
        pos.qty, pos.avg_price = 1.0, 100.0
        rm = RiskManager(account)
        d = rm.check(_sell(qty=99), price=100.0, calendar_window=False)
        assert d.quantity == 1.0


class TestR6Cash:
    def test_buy_trimmed_by_cash(self):
        account = Account(cash=300.0)
        rm = RiskManager(account)
        d = rm.check(_buy(qty=10), price=100.0, calendar_window=False)
        # 现金300 → 最多 3 个（加密步进0.0001不影响整数）
        assert d.quantity <= 3.0 + 1e-9
