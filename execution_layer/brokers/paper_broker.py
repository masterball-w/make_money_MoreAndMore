"""模拟盘 Broker —— 内存撮合：吃单按当前参考价全额成交，挂单进簿等价格触及。

用于：阶段1链路演示、回测、以及实盘前的长期策略验证。
撮合价格由注入的 price_provider 给出（演示用 MockMarketData 的最新价，
回测用历史价，实盘切换到交易所 ticker —— 三种场景同一个类）。

挂单（maker）语义：
  - place_order(order_type="limit") 只是把单挂进簿（返回 SUBMITTED，未成交）
  - check_fills(symbol, high, low, now) 由 K 线驱动：买单 low ≤ 限价 即成交于限价
    （保守口径：必须刺穿，不吃排队位置），卖单 high ≥ 限价 同理
  - 超时未成交自动撤销（maker_expiry_minutes）——错过就错过，策略下轮重新决策
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
        maker_expiry_minutes: float = 5.0,
    ) -> None:
        self._prices = price_provider
        self._slippage = slippage_bps / 10_000
        self.market = market
        self.knowledge = KnowledgeService()
        self.orders: dict[str, Order] = {}
        self._executed_client_ids: set[str] = set()
        # 挂单簿：order_id -> (order, placed_at_epoch)；placed_at 用秒数便于回测事件钟
        self._pending: dict[str, tuple[Order, float]] = {}
        self.maker_expiry_seconds = maker_expiry_minutes * 60

    async def place_order(self, order: Order, now_epoch: float | None = None) -> FillEvent:
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

        # 挂单：进簿等待（不立即成交，可能永远不成交）
        if order.order_type == "limit" and order.limit_price > 0:
            order.status = OrderStatus.SUBMITTED
            self.orders[order.order_id] = order
            self._pending[order.order_id] = (order, self._now_epoch(now_epoch))
            logger.info("[操作层|paper] 挂单 %s %s %.6g @ %.2f（%d 分钟内未成交将撤销）",
                        order.side.value.upper(), order.symbol, order.qty,
                        order.limit_price, self.maker_expiry_seconds // 60)
            return self._fill_from(order, OrderStatus.SUBMITTED)

        # 吃单：按参考价 ± 滑点全额成交
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

    def check_fills(self, symbol: str, high: float, low: float,
                    now_epoch: float | None = None) -> list[FillEvent]:
        """K 线驱动：检查挂单成交与过期。返回本轮新产生的成交事件（由调用方发布）。"""
        now = self._now_epoch(now_epoch)
        fills: list[FillEvent] = []
        for oid in list(self._pending):
            order, placed = self._pending[oid]
            if order.symbol != symbol:
                continue
            if now - placed > self.maker_expiry_seconds:
                order.status = OrderStatus.CANCELLED
                order.reason = "挂单超时未成交"
                del self._pending[oid]
                logger.info("[操作层|paper] 撤单 %s %s %.6g @ %.2f（超时）",
                            order.side.value.upper(), symbol, order.qty, order.limit_price)
                continue
            hit = (order.side == Action.BUY and low <= order.limit_price) or \
                  (order.side == Action.SELL and high >= order.limit_price)
            if not hit:
                continue
            # 成交于限价（不吃排队优势，保守口径），maker 费率
            rules = self.knowledge.market_rule(order.market)
            fee_rate = rules.maker_fee_rate if rules.maker_fee_rate is not None else rules.fee_rate
            order.filled_qty = order.qty
            order.avg_price = order.limit_price
            order.commission = order.limit_price * order.qty * fee_rate
            order.status = OrderStatus.FILLED
            del self._pending[oid]
            logger.info("[操作层|paper] 挂单成交 %s %s %.6g @ %.2f (maker 费率 %.4f%%)",
                        order.side.value.upper(), symbol, order.qty,
                        order.limit_price, fee_rate * 100)
            fills.append(self._fill_from(order))
        return fills

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @staticmethod
    def _now_epoch(now: float | None = None) -> float:
        import time as _time

        return now if now is not None else _time.time()

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
