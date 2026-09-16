"""操作层 + 全链路 · 模拟盘闭环测试：新闻 → 决策 → 成交 → 持仓。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.bus import EventBus
from common.events import Action, Market, MarketEvent, NewsCategory, Priority
from common.models import Account
from decision_layer.audit import AuditLog
from decision_layer.breakout_detector import BreakoutConfig, BreakoutDetector
from decision_layer.event_calendar import EconomicCalendar
from decision_layer.llm_analyzer import LLMAnalyzer
from decision_layer.quant_engine import QuantEngine
from decision_layer.risk_manager import RiskLimits, RiskManager
from decision_layer.strategy import StrategyConfig, StrategyOrchestrator
from execution_layer.brokers.paper_broker import PaperBroker
from execution_layer.executor import Executor
from info_layer.aggregator import InfoAggregator
from info_layer.base import RawNews
from info_layer.kline import MockKlineData
from info_layer.market_data import MockMarketData
from knowledge_layer.knowledge_service import KnowledgeService

SYMBOL = "BTC/USDT"


def _build(audit_path):
    bus = EventBus()
    account = Account(cash=100_000)
    risk = RiskManager(account)
    audit = AuditLog(audit_path)
    analyzer = LLMAnalyzer(KnowledgeService())          # 关键词模式（确定性）
    calendar = EconomicCalendar()                        # 空日历，不干扰
    strategy = StrategyOrchestrator(
        bus, analyzer, QuantEngine(), risk, calendar, audit,
        StrategyConfig(min_decision_interval=0.0))
    strategy.bind()
    prices: dict[str, float] = {}
    broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
    executor = Executor(bus, broker)
    executor.bind()
    aggregator = InfoAggregator(bus, sources=[], market_data={})
    market = MockMarketData(symbol=SYMBOL, drift=0.001, volatility=0.002, seed=7)
    return bus, account, risk, audit, aggregator, market, prices


class TestFullLoop:
    def test_news_to_fill_roundtrip(self, tmp_path):
        async def scenario():
            bus, account, risk, audit, agg, market, prices = _build(tmp_path / "a.db")
            # 行情预热（建立 market_cache 与指标）
            for _ in range(5):
                snap = await market.snapshot(SYMBOL)
                prices[SYMBOL] = snap.price
                await bus.publish(snap)
            cash_before = account.cash

            # 强利多 P1 新闻 → 应触发买入 → 成交 → 持仓增加
            await agg.ingest_raw(RawNews(
                title="美联储紧急降息100个基点",
                body="无限量宽松", source="t", category=NewsCategory.FED))
            await bus.wait_idle(0.2)

            assert any(p.qty > 0 for p in account.positions.values()), "应产生持仓"
            assert account.cash < cash_before, "买入应消耗现金"
            fills = audit.recent_fills(10)
            assert len(fills) >= 1
            assert fills[-1]["side"] == "buy"
            audit.close()

        asyncio.run(scenario())

    def test_idempotent_executor(self, tmp_path):
        async def scenario():
            bus, account, risk, audit, agg, market, prices = _build(tmp_path / "b.db")
            from common.events import DecisionEvent
            executed: list[str] = []
            from common.events import FillEvent

            async def spy(fill: FillEvent):
                executed.append(fill.order_id)

            bus.subscribe("fill", spy)
            d = DecisionEvent(action=Action.BUY, symbol=SYMBOL, market=Market.CRYPTO,
                              quantity=0.5, reference_price=100.0)
            prices[SYMBOL] = 100.0
            await bus.publish(d)
            await bus.publish(d)          # 重复发布同一决策 → 幂等拦截
            await bus.wait_idle(0.1)
            assert len(executed) == 1
            audit.close()

        asyncio.run(scenario())

    def test_dedup_no_double_decision(self, tmp_path):
        async def scenario():
            bus, account, risk, audit, agg, market, prices = _build(tmp_path / "c.db")
            for _ in range(5):
                snap = await market.snapshot(SYMBOL)
                prices[SYMBOL] = snap.price
                await bus.publish(snap)
            await agg.ingest_raw(RawNews(title="美联储降息", source="t"))
            await agg.ingest_raw(RawNews(title="美联储降息！", source="t2"))  # 重复
            await bus.wait_idle(0.2)
            decisions = audit.recent_decisions(50)
            # 同一指纹只进入决策一次：HOLD/交易决策里 news 触发的不超过 1 条链路
            assert len(decisions) < 3
            audit.close()

        asyncio.run(scenario())


class TestPaperBroker:
    def test_slippage_and_fee(self, tmp_path):
        async def scenario():
            from common.models import Order
            prices = {"BTC/USDT": 100.0}
            broker = PaperBroker(price_provider=lambda s: prices[s])
            fill = await broker.place_order(Order(
                order_id="o1", client_order_id="c1", symbol="BTC/USDT",
                market=Market.CRYPTO, side=Action.BUY, qty=1.0))
            assert fill.avg_price > 100.0          # 买入滑点向上
            assert fill.commission > 0             # 手续费
            assert fill.status.value == "filled"

        asyncio.run(scenario())


class TestKlineBreakoutLoop:
    def test_breakout_kline_triggers_buy(self, tmp_path):
        """实时 K 线突破技术指标 → SignalEvent → 决策 → 模拟盘成交。"""

        async def scenario():
            bus = EventBus()
            account = Account(cash=100_000)
            risk = RiskManager(account, RiskLimits(cooldown_minutes=0))
            audit = AuditLog(tmp_path / "k.db")
            strategy = StrategyOrchestrator(
                bus, LLMAnalyzer(KnowledgeService()), QuantEngine(), risk,
                EconomicCalendar(), audit,
                StrategyConfig(min_decision_interval=0.0))
            strategy.bind()
            detector = BreakoutDetector(bus, BreakoutConfig())
            detector.bind()
            prices: dict[str, float] = {}
            broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
            Executor(bus, broker).bind()

            klines = MockKlineData(symbol=SYMBOL, drift=0.0, volatility=0.001,
                                   seed=11, ticks_per_candle=2)
            # 预热：推足 K 线建立指标状态
            for _ in range(20):
                ev = await klines.next(SYMBOL)
                prices[SYMBOL] = ev.closes()[-1]
                await bus.publish(ev)
            assert not any(p.qty > 0 for p in account.positions.values()), "横盘不应开仓"

            # 注入连续急涨 K 线 → 突破 Donchian/布林 → 应产生买入
            base = prices[SYMBOL]
            for i in range(4):
                klines.inject_candle(round(base * (1.05 ** (i + 1)), 2))
                ev = await klines.next(SYMBOL)
                prices[SYMBOL] = ev.closes()[-1]
                await bus.publish(ev)
            await bus.wait_idle(0.2)

            assert any(p.qty > 0 for p in account.positions.values()), "突破应触发买入"
            fills = audit.recent_fills(10)
            assert any(f["side"] == "buy" for f in fills)
            # 决策理由应包含突破来源
            buys = [d for d in audit.recent_decisions(20)
                    if d["action"] == "buy" and d["quantity"] > 0]
            assert buys and ("donch" in buys[-1]["reason"] or "boll" in buys[-1]["reason"]
                             or "macd" in buys[-1]["reason"])
            audit.close()

        asyncio.run(scenario())
