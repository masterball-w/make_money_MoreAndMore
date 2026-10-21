"""挂单（maker）撮合机制测试 —— PaperBroker 挂单簿 + Executor 流转。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.bus import EventBus
from common.events import Action, DecisionEvent, FillEvent, Market, OrderStatus
from execution_layer.brokers.paper_broker import PaperBroker
from execution_layer.executor import Executor

SYMBOL = "BTC/USDT"


def _order(side=Action.BUY, qty=0.01, limit=0.0, otype="market", oid="o1"):
    from common.models import Order
    return Order(order_id=oid, client_order_id=f"c-{oid}", symbol=SYMBOL,
                 market=Market.CRYPTO, side=side, qty=qty,
                 order_type=otype, limit_price=limit)


class TestPaperBrokerMaker:
    def test_limit_order_waits_then_fills_on_touch(self):
        async def scenario():
            prices = {SYMBOL: 100.0}
            broker = PaperBroker(price_provider=lambda s: prices[s])
            ack = await broker.place_order(_order(limit=99.0, otype="limit"))
            assert ack.status == OrderStatus.SUBMITTED, "挂单应先返回已提交而非成交"
            # 未触及：不成交
            fills = broker.check_fills(SYMBOL, high=100.5, low=99.5, now_epoch=1000.0)
            assert fills == []
            # 买单：low ≤ 限价 → 成交于限价，maker 费率
            fills = broker.check_fills(SYMBOL, high=100.5, low=98.9, now_epoch=1001.0)
            assert len(fills) == 1
            assert fills[0].avg_price == 99.0
            assert fills[0].commission < 99.0 * 0.01 * 0.001, "应使用更低的 maker 费率"

        asyncio.run(scenario())

    def test_limit_order_expires(self):
        async def scenario():
            prices = {SYMBOL: 100.0}
            broker = PaperBroker(price_provider=lambda s: prices[s],
                                 maker_expiry_minutes=1.0)
            await broker.place_order(_order(limit=90.0, otype="limit", oid="o2"),
                                     now_epoch=1000.0)
            # 61 分钟后（远超有效期）仍未触及 → 撤销而非成交
            fills = broker.check_fills(SYMBOL, high=95.0, low=91.0, now_epoch=4660.0)
            assert fills == []
            assert broker.pending_count == 0, "超时挂单应被撤销移出挂单簿"

        asyncio.run(scenario())

    def test_sell_limit_fills_on_high(self):
        async def scenario():
            prices = {SYMBOL: 100.0}
            broker = PaperBroker(price_provider=lambda s: prices[s])
            await broker.place_order(_order(side=Action.SELL, qty=0.01, limit=101.0,
                                            otype="limit", oid="o3"))
            fills = broker.check_fills(SYMBOL, high=101.2, low=100.1, now_epoch=1000.0)
            assert len(fills) == 1 and fills[0].side == Action.SELL
            assert fills[0].avg_price == 101.0

        asyncio.run(scenario())


class TestExecutorMakerFlow:
    def test_submitted_not_published_fill_comes_via_kline(self):
        async def scenario():
            bus = EventBus()
            prices = {SYMBOL: 100.0}
            broker = PaperBroker(price_provider=lambda s: prices[s])
            executor = Executor(bus, broker)
            executor.bind()
            published: list[FillEvent] = []

            async def spy(fill: FillEvent):
                published.append(fill)

            bus.subscribe("fill", spy)

            from common.events import Candle, KlineEvent
            from datetime import datetime, timezone

            decision = DecisionEvent(action=Action.BUY, symbol=SYMBOL,
                                     market=Market.CRYPTO, quantity=0.01,
                                     reference_price=100.0, maker=True)
            await bus.publish(decision)
            assert published == [], "挂单提交阶段不应发布成交"

            # K 线回落触及限价 100.0 → 撮合发布成交
            k = KlineEvent(symbol=SYMBOL, market=Market.CRYPTO, interval="1m",
                           current=Candle(ts=datetime.now(timezone.utc), open=100.5,
                                          high=100.6, low=99.8, close=100.1,
                                          volume=1.0, closed=False))
            await bus.publish(k)
            assert len(published) == 1
            assert published[0].status == OrderStatus.FILLED
            assert published[0].avg_price == 100.0

        asyncio.run(scenario())
