"""订单执行器 —— 操作层的编排入口：订阅决策 → 幂等下单 → 回报发布。

防重复下单：decision_id 做幂等键（同一决策只会产生一笔订单）。
执行失败（broker 异常/拒单）不抛出到总线，只记日志与审计 —— 下一轮
决策会基于最新持仓状态重算，不盲目重试。

挂单（maker）流转：决策带 maker=True → 下限价单（成交回报延后由
K 线驱动 check_fills 产生）→ 本模块订阅 kline 逐根检查并发布成交。
"""
from __future__ import annotations

import hashlib
import logging
import uuid

from common.bus import EventBus
from common.events import Action, DecisionEvent, FillEvent, KlineEvent, OrderStatus
from common.models import Order
from execution_layer.brokers.base import Broker

logger = logging.getLogger("executor")


class Executor:
    def __init__(self, bus: EventBus, broker: Broker) -> None:
        self.bus = bus
        self.broker = broker
        self._executed_decisions: set[str] = set()

    def bind(self) -> None:
        self.bus.subscribe("decision", self.on_decision)
        self.bus.subscribe("kline", self.on_kline)
        logger.info("操作层已订阅 decision/kline（broker=%s）", self.broker.name)

    async def on_decision(self, decision: DecisionEvent) -> None:
        if decision.action == Action.HOLD or decision.quantity <= 0:
            return
        if decision.id in self._executed_decisions:
            logger.warning("重复决策被幂等拦截: %s", decision.id)
            return
        self._executed_decisions.add(decision.id)

        order = Order(
            order_id=uuid.uuid4().hex[:12],
            client_order_id=_idempotency_key(decision),
            symbol=decision.symbol,
            market=decision.market,
            side=decision.action,
            qty=decision.quantity,
        )
        # 挂单进场：限价挂参考价（省 maker 费率）；离场决策永远吃单
        if decision.maker and decision.reference_price > 0:
            order.order_type = "limit"
            order.limit_price = round(decision.reference_price, 2)
        try:
            fill: FillEvent = await self.broker.place_order(order)
        except Exception:  # noqa: BLE001 —— 执行失败：不重试，等待下一轮决策
            logger.exception("下单失败: %s %s", decision.action.value, decision.symbol)
            return

        fill.decision_id = decision.id
        if fill.status == OrderStatus.SUBMITTED:
            logger.info("[操作层] 挂单已进簿: %s %.6g @ %.2f（等待K线驱动撮合）",
                        decision.symbol, decision.quantity, order.limit_price)
            return                          # 成交回报由 on_kline 后续发布
        if fill.status.value == "rejected":
            logger.error("[操作层] 订单被拒: %s %s", decision.symbol, decision.quantity)
            return
        await self.bus.publish(fill)

    async def on_kline(self, event: KlineEvent) -> None:
        """K 线驱动挂单撮合与过期撤销（模拟盘挂单簿）。"""
        check = getattr(self.broker, "check_fills", None)
        if check is None:
            return
        candle = event.current if event.current is not None else (
            event.history[-1] if event.history else None)
        if candle is None:
            return
        # 事件时间作撮合时钟（回测=事件时间，实盘≈墙钟）
        fills = check(event.symbol, candle.high, candle.low, event.ts.timestamp())
        for fill in fills:
            await self.bus.publish(fill)


def _idempotency_key(decision: DecisionEvent) -> str:
    raw = f"{decision.id}:{decision.action.value}:{decision.symbol}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
