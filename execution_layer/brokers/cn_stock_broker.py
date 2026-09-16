"""A股实盘 Broker —— easytrader / QMT 封装（阶段6启用，当前为占位实现）。

合规提示：A股程序化交易须遵守交易所报备新规；本系统只做低频（小时级/日级）
决策，严禁高频报单。T+1 制度下当日买入不可卖出 —— 实装时须在此处强制校验。
"""
from __future__ import annotations

import logging

from common.events import FillEvent, Market, OrderStatus
from common.models import Order
from execution_layer.brokers.base import Broker

logger = logging.getLogger("cn_stock_broker")


class CNStockBroker(Broker):
    name = "cn-stock-live"
    market = Market.CN_STOCK

    def __init__(self, broker_client=None) -> None:
        """broker_client: easytrader 的 client 实例（阶段6注入）。"""
        self.client = broker_client
        self.orders: dict[str, Order] = {}

    async def place_order(self, order: Order) -> FillEvent:
        if self.client is None:
            raise NotImplementedError(
                "A股实盘需注入 easytrader/QMT 客户端（阶段6）。当前请使用 paper 模式。")
        raise NotImplementedError("阶段6实装：对接券商客户端并处理 T+1 与涨跌停校验")

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("阶段6实装")

    def order_status(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    async def fetch_price(self, symbol: str) -> float:
        raise NotImplementedError("阶段2接入 akshare 行情")
