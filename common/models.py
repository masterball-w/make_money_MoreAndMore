"""数据模型 —— 账户、持仓、订单。"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.events import Action, Market, OrderStatus


@dataclass
class Position:
    symbol: str
    market: Market
    qty: float = 0.0
    avg_price: float = 0.0

    def apply_fill(self, side: Action, qty: float, price: float) -> None:
        """按成交更新持仓（买入加权平均成本，卖出数量减少）。"""
        if side == Action.BUY:
            total_cost = self.avg_price * self.qty + price * qty
            self.qty += qty
            self.avg_price = total_cost / self.qty if self.qty else 0.0
        else:  # SELL
            self.qty = max(0.0, self.qty - qty)
            if self.qty == 0.0:
                self.avg_price = 0.0

    def market_value(self, price: float) -> float:
        return self.qty * price


@dataclass
class Account:
    """本地维护的账户视图（由 FillEvent 驱动更新，操作层负责与券商对账）。"""

    base_currency: str = "USDT"
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    # 风控相关
    high_water_mark: float = 0.0           # 净值高水位（回撤熔断基准）
    halted: bool = False                   # 全局熔断标志，人工解锁

    def position(self, symbol: str, market: Market) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol, market=market)
        return self.positions[symbol]

    def equity(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            pos.market_value(prices.get(pos.symbol, pos.avg_price))
            for pos in self.positions.values()
        )
        return self.cash + pos_value

    def update_water_mark(self, equity: float) -> float:
        """返回当前回撤比例（相对高水位）。"""
        self.high_water_mark = max(self.high_water_mark, equity)
        if self.high_water_mark <= 0:
            return 0.0
        return (self.high_water_mark - equity) / self.high_water_mark


@dataclass
class Order:
    """订单（含幂等键 client_order_id，防止重复下单）。"""

    order_id: str = ""
    client_order_id: str = ""
    symbol: str = ""
    market: Market = Market.CRYPTO
    side: Action = Action.BUY
    qty: float = 0.0
    filled_qty: float = 0.0
    avg_price: float = 0.0
    commission: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    reason: str = ""
