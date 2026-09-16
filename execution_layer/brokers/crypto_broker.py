"""加密货币实盘 Broker —— ccxt 封装（阶段6启用，当前为占位实现）。

启用前置条件（缺一不可）：
1. pip install ccxt
2. config/settings.yaml 配置 exchange/api_key/secret
3. mode: live 且完成二次确认
4. 回测达标 + 模拟盘至少运行 N 周
"""
from __future__ import annotations

import logging
import uuid

from common.events import Action, FillEvent, Market, OrderStatus
from common.models import Order
from execution_layer.brokers.base import Broker

logger = logging.getLogger("crypto_broker")


class CryptoBroker(Broker):
    name = "crypto-live"
    market = Market.CRYPTO

    def __init__(self, exchange_id: str = "binance", api_key: str = "", secret: str = "") -> None:
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.secret = secret
        self.orders: dict[str, Order] = {}

    def _exchange(self):  # 懒加载 ccxt
        try:
            import ccxt.async_support as ccxt  # type: ignore[import-untyped]
        except ImportError as e:
            raise NotImplementedError("pip install ccxt 后启用实盘") from e
        return getattr(ccxt, self.exchange_id)({"apiKey": self.api_key, "secret": self.secret})

    async def place_order(self, order: Order) -> FillEvent:
        ex = self._exchange()
        try:
            resp = await ex.create_order(
                symbol=order.symbol,
                type="market",
                side=self.side_str(order.side),
                amount=order.qty,
                params={"clientOrderId": order.client_order_id or uuid.uuid4().hex},
            )
            order.status = OrderStatus.FILLED
            order.filled_qty = float(resp.get("filled") or order.qty)
            order.avg_price = float(resp.get("average") or resp.get("price") or 0.0)
            fee_info = resp.get("fee") or {}
            order.commission = float(fee_info.get("cost") or 0.0)
            self.orders[order.order_id] = order
            return FillEvent(
                order_id=order.order_id, symbol=order.symbol, market=self.market,
                side=order.side, filled_qty=order.filled_qty, avg_price=order.avg_price,
                commission=order.commission, status=order.status)
        finally:
            await ex.close()

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("阶段6实装")

    def order_status(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    async def fetch_price(self, symbol: str) -> float:
        ex = self._exchange()
        try:
            t = await ex.fetch_ticker(symbol)
            return float(t["last"])
        finally:
            await ex.close()
