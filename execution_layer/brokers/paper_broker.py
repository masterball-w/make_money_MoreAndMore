"""模拟盘 Broker —— 内存撮合：按当前参考价全额成交，收手续费。

用于：阶段1链路演示、回测、以及实盘前的长期策略验证。
撮合价格由注入的 price_provider 给出（演示用 MockMarketData 的最新价，
回测用历史价，实盘切换到交易所 ticker —— 三种场景同一个类）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from common.events import Action, FillEvent, Market, OrderStatus
from common.models import Order
from execution_layer.brokers.base import Broker
from knowledge_layer.knowledge_service import KnowledgeService

logger = logging.getLogger("paper_broker")


class PaperBroker(Broker):
    name = "paper"
    market = Market.CRYPTO

    def __init__(
        self,
        price_provider: Callable[[str], float],
        slippage_bps: float = 2.0,
        market: Market = Market.CRYPTO,
    ) -> None:
        self._prices = price_provider
        self._slippage = slippage_bps / 10_000
        self.market = market
        self.knowledge = KnowledgeService()
        self.orders: dict[str, Order] = {}
        self._executed_client_ids: set[str] = set()

    async def place_order(self, order: Order) -> FillEvent:
        # 幂等：同一 client_order_id 直接返回已存在的结果
        if order.client_order_id in self._executed_client_ids:
            existing = next(
                (o for o in self.orders.values()
                 if o.client_order_id == order.client_order_id and o.status == OrderStatus.FILLED),
                None,
            )
            if existing:
                return self._fill_from(existing)
        self._executed_client_ids.add(order.client_order_id)

        ref = self._prices(order.symbol)
        if ref <= 0:
            order.status, order.reason = OrderStatus.REJECTED, "无参考价"
            self.orders[order.order_id] = order
            return self._fill_from(order, OrderStatus.REJECTED)

        # 滑点：买入略高、卖出略低
        slip = self._slippage if order.side == Action.BUY else -self._slippage
        fill_price = round(ref * (1 + slip), 2)
        fee = fill_price * order.qty * self.knowledge.market_rule(order.market).fee_rate

        order.filled_qty = order.qty
        order.avg_price = fill_price
        order.commission = fee
        order.status = OrderStatus.FILLED
        self.orders[order.order_id] = order

        logger.info("[操作层|paper] 成交 %s %s %.6g @ %.2f (费 %.4f)",
                    order.side.value.upper(), order.symbol, order.qty, fill_price, fee)
        return self._fill_from(order)

    async def cancel_order(self, order_id: str) -> bool:
        order = self.orders.get(order_id)
        if order and order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            order.status = OrderStatus.CANCELLED
            return True
        return False

    def order_status(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    async def fetch_price(self, symbol: str) -> float:
        return self._prices(symbol)

    def _fill_from(self, order: Order, status: OrderStatus | None = None) -> FillEvent:
        return FillEvent(
            decision_id="",
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            filled_qty=order.filled_qty,
            avg_price=order.avg_price,
            commission=order.commission,
            status=status or order.status,
        )
