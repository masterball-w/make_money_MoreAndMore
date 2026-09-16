"""事件回放回测引擎。

核心思想：实盘是"事件 → 决策 → 成交"的流，回测就是把历史事件按时间序
重新灌入同一套 Strategy + PaperBroker。因为决策层只依赖事件（不依赖墙钟），
回测与实盘是同一份代码 —— 这是防止"回测好看实盘拉胯"的第一道防线。

时间控制：回测时把 RiskManager/Strategy 里的 now() 换成事件时间（见 _clock）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from common.bus import EventBus
from common.events import Event, MarketEvent
from common.models import Account
from decision_layer.audit import AuditLog
from decision_layer.event_calendar import EconomicCalendar
from decision_layer.llm_analyzer import LLMAnalyzer
from decision_layer.quant_engine import QuantEngine
from decision_layer.risk_manager import RiskManager
from decision_layer.strategy import StrategyConfig, StrategyOrchestrator
from execution_layer.brokers.paper_broker import PaperBroker
from execution_layer.executor import Executor
from knowledge_layer.knowledge_service import KnowledgeService

logger = logging.getLogger("backtest")


@dataclass
class BacktestResult:
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[datetime] = field(default_factory=list)
    n_decisions: int = 0
    n_fills: int = 0
    final_equity: float = 0.0

    def summary(self) -> dict:
        k = KnowledgeService()
        rets = k.risk.simple_returns(self.equity_curve)
        mdd, _, _ = k.risk.max_drawdown(self.equity_curve)
        return {
            "期末净值": round(self.final_equity, 2),
            "总收益%": round((self.final_equity / self.equity_curve[0] - 1) * 100, 2) if self.equity_curve else 0.0,
            "年化波动率%": round(k.risk.annualized_volatility(rets) * 100, 2),
            "最大回撤%": round(mdd * 100, 2),
            "夏普比率": round(k.risk.sharpe_ratio(rets), 3),
            "决策数": self.n_decisions,
            "成交数": self.n_fills,
        }


class BacktestRunner:
    def __init__(
        self,
        events: list[Event],                      # 按时间排好序的历史事件
        initial_cash: float = 100_000.0,
        strategy_config: StrategyConfig | None = None,
        audit_path: str = "data/backtest_audit.db",
    ) -> None:
        self.events = sorted(events, key=lambda e: e.ts)
        self.account = Account(cash=initial_cash)
        self.bus = EventBus()
        self.audit = AuditLog(audit_path)
        self.analyzer = LLMAnalyzer(KnowledgeService())   # 回测默认关键词模式（可换 LLM）
        self.quant = QuantEngine()
        self.risk = RiskManager(self.account)
        self.calendar = EconomicCalendar()
        self.strategy = StrategyOrchestrator(
            self.bus, self.analyzer, self.quant, self.risk, self.calendar,
            self.audit, strategy_config)
        prices: dict[str, float] = {}
        self.broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
        self.executor = Executor(self.bus, self.broker)
        self._prices = prices
        self.result = BacktestResult(equity_curve=[initial_cash])

    async def run(self) -> BacktestResult:
        self.strategy.bind()
        self.executor.bind()
        self.result.timestamps.append(self.events[0].ts if self.events else datetime.now())

        for i, event in enumerate(self.events):
            if isinstance(event, MarketEvent):
                self._prices[event.symbol] = event.price
            await self.bus.publish(event)
            # 每个事件后记录净值
            equity = self.account.equity(self._prices)
            if equity != self.result.equity_curve[-1]:
                self.result.equity_curve.append(equity)
                self.result.timestamps.append(event.ts)
            await asyncio.sleep(0)  # 让出事件循环

        self.result.n_decisions = len(self.audit.recent_decisions(limit=10_000))
        self.result.n_fills = len(self.audit.recent_fills(limit=10_000))
        self.result.final_equity = self.account.equity(self._prices)
        self.audit.close()
        return self.result
