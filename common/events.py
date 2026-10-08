"""事件定义 —— 四层之间唯一的数据交换格式。

事件驱动架构的核心约定：
    信息层  --publish--> news / market 主题
    决策层  --subscribe--> news / market；--publish--> decision
    操作层  --subscribe--> decision；--publish--> fill
任何一层不直接 import 另一层的实现，只认识这里的事件。
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- 通用枚举


class Market(enum.Enum):
    CRYPTO = "crypto"
    CN_STOCK = "cn_stock"


class Priority(enum.Enum):
    """新闻重要性分级：P1 紧急（议息决议/战争爆发）、P2 重要、P3 常规。"""

    P1 = 1
    P2 = 2
    P3 = 3


class NewsCategory(enum.Enum):
    FED = "fed"            # 美联储/各国央行
    GEO = "geo"            # 地缘政治（中东战争等）
    AI = "ai"              # AI 大模型
    CRYPTO = "crypto"      # 加密行业
    CN_FINANCE = "cn"      # 国内财经


class Action(enum.Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class OrderStatus(enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# ---------------------------------------------------------------- 基类


@dataclass
class Event:
    id: str = field(default_factory=_uid)
    ts: datetime = field(default_factory=_now)

    def topic(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------- 信息层产出


@dataclass
class NewsEvent(Event):
    """一条去重、分级、关联标的之后的新闻。"""

    title: str = ""
    body: str = ""
    url: str = ""
    source: str = ""                       # 来源名，如 "fed-rss"
    published_at: datetime = field(default_factory=_now)
    category: NewsCategory = NewsCategory.CRYPTO
    priority: Priority = Priority.P3
    symbols: list[str] = field(default_factory=list)   # 关联标的，如 ["BTC/USDT"]
    fingerprint: str = ""                  # 内容指纹（去重用）

    def topic(self) -> str:
        return "news"


@dataclass
class MarketEvent(Event):
    """一次行情快照 / K 线更新。"""

    symbol: str = ""
    market: Market = Market.CRYPTO
    price: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0
    history: list[float] = field(default_factory=list)  # 最近收盘价序列（供指标计算）

    def topic(self) -> str:
        return "market"


@dataclass
class Candle:
    """一根 K 线（OHLCV）。closed=False 表示当前正在形成的未收盘 K 线。"""

    ts: datetime = field(default_factory=_now)
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    closed: bool = True


@dataclass
class KlineEvent(Event):
    """实时 K 线流事件：当前 K 线（可能未收盘）+ 已收盘历史 K 线序列。"""

    symbol: str = ""
    market: Market = Market.CRYPTO
    interval: str = "1m"                      # K 线周期
    current: Candle | None = None
    history: list[Candle] = field(default_factory=list)   # 旧 → 新，均为已收盘 K 线

    def topic(self) -> str:
        return "kline"

    def closes(self) -> list[float]:
        """含当前 K 线最新价的收盘价序列（供指标计算）。"""
        base = [c.close for c in self.history]
        if self.current is not None:
            base.append(self.current.close)
        return base


@dataclass
class SignalEvent(Event):
    """技术突破信号 —— 指标穿越阈值的事件（如 MACD 金叉、突破 20 根 K 线高点）。

    由决策层的突破检测器/资金流分析器产出，strategy 订阅后立即触发决策评估。
    kind 约定前缀：macd_* / boll_* / donch_*（技术突破）、flow_*（资金流异常）。
    """

    symbol: str = ""
    market: Market = Market.CRYPTO
    kind: str = ""              # macd_golden / donch_up / flow_in / flow_out ...
    direction: float = 0.0      # +1 看多 / -1 看空
    strength: float = 0.0       # 0~1，突破幅度/ATR 或 z-score 归一化
    detail: str = ""
    indicator: float = 0.0      # 触发时的指标值
    threshold: float = 0.0      # 被穿越的阈值
    interval: str = "1m"

    def topic(self) -> str:
        return "signal"


@dataclass
class FlowEvent(Event):
    """资金流事件 —— 周期内主动买/卖量（信息层资金流源产出）。

    数据来源：交易所 taker volume（主动买入吃单量 vs 主动卖出挂单成交量）。
    net_flow > 0 表示资金净流入（买方更激进），< 0 表示净流出/抛售。
    """

    symbol: str = ""
    market: Market = Market.CRYPTO
    buy_volume: float = 0.0     # 周期内主动买量
    sell_volume: float = 0.0    # 周期内主动卖量
    net_flow: float = 0.0       # buy - sell
    total_volume: float = 0.0   # buy + sell
    period: str = "5m"          # 统计周期
    open_interest: float | None = None   # 合约持仓量（可选，OI 骤变=大资金进出）

    def topic(self) -> str:
        return "flow"


# ---------------------------------------------------------------- 决策层产出


@dataclass
class RiskCheck:
    """风控门禁的单项检查记录（审计用）。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class DecisionEvent(Event):
    """决策层产出、操作层执行的交易决策。"""

    action: Action = Action.HOLD
    symbol: str = ""
    market: Market = Market.CRYPTO
    quantity: float = 0.0                  # 数量；0 表示无操作
    reference_price: float = 0.0           # 决策时的参考价
    confidence: float = 0.0                # 0~1
    reason: str = ""
    # 输入快照（审计）
    llm_sentiment: float | None = None     # -1~1
    llm_reasoning: str = ""
    quant_signal: float | None = None      # -1~1
    news_ids: list[str] = field(default_factory=list)
    risk_checks: list[RiskCheck] = field(default_factory=list)
    blocked_by_risk: bool = False          # 被风控否决/削减时置 True

    def topic(self) -> str:
        return "decision"


# ---------------------------------------------------------------- 操作层产出


@dataclass
class FillEvent(Event):
    """订单成交/拒绝回报，回流决策层更新持仓与风控状态。"""

    decision_id: str = ""
    order_id: str = ""
    symbol: str = ""
    market: Market = Market.CRYPTO
    side: Action = Action.BUY
    filled_qty: float = 0.0
    avg_price: float = 0.0
    commission: float = 0.0
    status: OrderStatus = OrderStatus.FILLED

    def topic(self) -> str:
        return "fill"
