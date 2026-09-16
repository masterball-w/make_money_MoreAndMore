"""Broker 抽象 —— 操作层的统一交易接口。

实现：PaperBroker（模拟盘）/ CryptoBroker（ccxt，阶段6）/ CNStockBroker（阶段6）。
切换市场或实/模拟，决策层与执行器代码零改动。
"""
from __future__ import annotations

import abc

from common.events import Action, FillEvent, Market
from common.models import Order


class Broker(abc.ABC):
    name: str = "base"
    market: Market = Market.CRYPTO

    @abc.abstractmethod
    async def place_order(self, order: Order) -> FillEvent:
        """提交订单并等待结果。实现必须幂等：同一 client_order_id 只执行一次。"""

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """撤单（未成交部分）。"""

    @abc.abstractmethod
    def order_status(self, order_id: str) -> Order | None:
        """查询订单状态。"""

    @abc.abstractmethod
    async def fetch_price(self, symbol: str) -> float:
        """最新成交价（模拟盘撮合定价用）。"""

    def side_str(self, side: Action) -> str:
        return "buy" if side == Action.BUY else "sell"
