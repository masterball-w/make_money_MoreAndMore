"""资金流检测单元测试：公式 / 异常触发 / 去抖 / 量价关系 / 端到端脉冲。"""
import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.bus import EventBus
from common.events import Action, Candle, KlineEvent, Market, FlowEvent
from common.models import Account
from decision_layer.audit import AuditLog
from decision_layer.event_calendar import EconomicCalendar
from decision_layer.flow_analyzer import FlowAnalyzer, FlowConfig
from decision_layer.llm_analyzer import LLMAnalyzer
from decision_layer.quant_engine import QuantEngine
from decision_layer.risk_manager import RiskLimits, RiskManager
from decision_layer.strategy import StrategyConfig, StrategyOrchestrator
from execution_layer.brokers.paper_broker import PaperBroker
from execution_layer.executor import Executor
from info_layer.flow import MockFlowSource
from knowledge_layer.formulas import flow as fm
from knowledge_layer.knowledge_service import KnowledgeService

SYMBOL = "BTC/USDT"


class TestFlowFormulas:
    def test_volume_imbalance(self):
        assert fm.volume_imbalance(100, 0) == 1.0        # 纯买
        assert fm.volume_imbalance(0, 100) == -1.0       # 纯卖
        assert math.isclose(fm.volume_imbalance(60, 40), 0.2)
        assert fm.volume_imbalance(0, 0) == 0.0

    def test_zscore(self):
        assert fm.zscore(110, 100, 5) == 2.0
        assert fm.zscore(100, 100, 5) == 0.0
        assert fm.zscore(100, 100, 0) == 0.0             # 一致 → 无异常
        assert fm.zscore(110, 100, 0) == 10.0            # 零波动基线上的偏离 → 极端显著
        assert fm.zscore(90, 100, 0) == -10.0

    def test_rolling_zscore(self):
        # 带噪声历史中的正常值 → 低 z
        hist = [10.0 + 0.2 * math.sin(i) for i in range(20)]
        assert abs(fm.rolling_zscore(10.1, hist)) < 1.0
        # 极端值 → 高 z
        assert abs(fm.rolling_zscore(20.0, hist)) > 3
        # 零波动基线中的偏离 → 极限 z
        assert abs(fm.rolling_zscore(10.5, [10.0] * 20)) >= 10


def _flow(buy: float, sell: float) -> FlowEvent:
    return FlowEvent(symbol=SYMBOL, market=Market.CRYPTO, buy_volume=buy,
                     sell_volume=sell, net_flow=buy - sell,
                     total_volume=buy + sell, period="5m")


def _kline(close: float) -> KlineEvent:
    import datetime as dt
    hist = [Candle(ts=dt.datetime(2026, 1, 1, 0, i, tzinfo=dt.timezone.utc),
                   open=close, high=close, low=close, close=close) for i in range(30)]
    return KlineEvent(symbol=SYMBOL, market=Market.CRYPTO, current=None, history=hist)


class TestFlowAnalyzer:
    def _detector(self):
        bus = EventBus()
        signals: list = []

        async def spy(s):
            signals.append(s)
        bus.subscribe("signal", spy)
        return bus, signals, FlowAnalyzer(bus, FlowConfig(min_history=10, window=20,
                                                          cooldown_minutes=0.01))

    def test_stable_flow_no_signal(self):
        async def scenario():
            import random
            rng = random.Random(3)
            bus, signals, fa = self._detector()
            for _ in range(20):
                total = rng.uniform(9, 11)
                imb = rng.uniform(-0.05, 0.05)
                await fa.on_flow(_flow(total * (1 + imb) / 2, total * (1 - imb) / 2))
            assert signals == []
        asyncio.run(scenario())

    def test_big_inflow_triggers_flow_in(self):
        async def scenario():
            bus, signals, fa = self._detector()
            for _ in range(15):                    # 平稳基线
                await fa.on_flow(_flow(5.0, 5.0))
            await fa.on_flow(_flow(50.0, 5.0))     # 买方异常激进
            assert len(signals) == 1
            s = signals[0]
            assert s.kind == "flow_in" and s.direction == +1
            assert s.strength > 0.2
        asyncio.run(scenario())

    def test_big_outflow_triggers_flow_out(self):
        async def scenario():
            bus, signals, fa = self._detector()
            for _ in range(15):
                await fa.on_flow(_flow(5.0, 5.0))
            await fa.on_flow(_flow(5.0, 50.0))     # 大额抛售
            assert signals and signals[0].kind == "flow_out" and signals[0].direction == -1
        asyncio.run(scenario())

    def test_divergence_halves_strength(self):
        """流入但价格下跌 → 量价背离 → 强度减半（对比无价格数据时）。"""
        async def scenario():
            bus, s1, fa1 = self._detector()
            for _ in range(15):
                await fa1.on_flow(_flow(5.0, 5.0))
            await fa1.on_flow(_flow(50.0, 5.0))    # 无价格信息
            base = s1[0].strength

            bus2, s2, fa2 = self._detector()
            for _ in range(15):
                await fa2.on_kline(_kline(100.0))
                await fa2.on_flow(_flow(5.0, 5.0))
            await fa2.on_kline(_kline(98.0))       # 价格下跌
            await fa2.on_flow(_flow(50.0, 5.0))    # 流入但跌价 → 背离
            assert s2[0].strength < base
            assert "背离" in s2[0].detail
        asyncio.run(scenario())


class TestMockFlowSource:
    def test_pulse_injection(self):
        async def scenario():
            src = MockFlowSource(symbol=SYMBOL, seed=1)
            normal = await src.next(SYMBOL)
            assert abs(normal.net_flow) / normal.total_volume < 0.2   # 平稳
            src.inject_pulse(imbalance=-0.9, volume_mult=5.0)
            pulse = await src.next(SYMBOL)
            assert pulse.sell_volume > pulse.buy_volume * 5
            assert fm.volume_imbalance(pulse.buy_volume, pulse.sell_volume) < -0.8
        asyncio.run(scenario())


class TestFlowE2E:
    def test_sell_pulse_clears_position(self, tmp_path):
        """大额抛售脉冲 → flow_out → 持仓清空（资金流驱动操作）。"""

        async def scenario():
            bus = EventBus()
            account = Account(cash=100_000)
            account.high_water_mark = 100_000
            risk = RiskManager(account, RiskLimits(cooldown_minutes=0))
            audit = AuditLog(tmp_path / "f.db")
            strategy = StrategyOrchestrator(
                bus, LLMAnalyzer(KnowledgeService()), QuantEngine(), risk,
                EconomicCalendar(), audit, StrategyConfig(min_decision_interval=0.0))
            strategy.bind()
            FlowAnalyzer(bus, FlowConfig(min_history=10, window=20)).bind()
            prices: dict[str, float] = {}
            broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
            Executor(bus, broker).bind()

            flows = MockFlowSource(symbol=SYMBOL, seed=5)
            # 建立 K 线行情缓存（决策评估需要行情输入）
            prices[SYMBOL] = 100.0
            for i in range(3):
                await bus.publish(_kline(100.0 + i * 0.1))
            await bus.wait_idle(0.1)
            # 建立持仓：直接给一个已过风控的买入决策
            from common.events import DecisionEvent
            await bus.publish(DecisionEvent(action=Action.BUY, symbol=SYMBOL,
                                            market=Market.CRYPTO, quantity=1.0,
                                            reference_price=100.0))
            await bus.wait_idle(0.1)
            assert account.position(SYMBOL, Market.CRYPTO).qty > 0

            # 平稳资金流 → 无信号
            for _ in range(12):
                await bus.publish(await flows.next(SYMBOL))
            await bus.wait_idle(0.1)

            # 大额抛售脉冲 → flow_out → 清仓
            flows.inject_pulse(imbalance=-0.9, volume_mult=5.0)
            await bus.publish(await flows.next(SYMBOL))
            await bus.wait_idle(0.2)
            assert account.position(SYMBOL, Market.CRYPTO).qty == 0, "抛售脉冲应清仓"
            audit.close()
        asyncio.run(scenario())
