"""风险管理器 —— 决策层的守门员，所有 DecisionEvent 必须过这道门才能到操作层。

风控规则（参数见 config/risk_limits.yaml）：
R1 单笔风险   ：下单金额 ≤ 账户净值 * max_risk_per_trade
R2 单标的仓位 ：该标的总市值 ≤ 账户净值 * max_position_pct
R3 回撤熔断   ：账户回撤 ≥ max_drawdown_halt → halted=True，一切新开仓被拒，
                需人工调用 unlock()（这是"亏到线就停"的硬约束）
R4 事件窗口   ：重大经济事件公布前 event_window_minutes 内，新开仓数量减半
R5 冷却期     ：同一标的 last_trade 后 cooldown_minutes 内不开新仓（防新闻反复触发）
R6 现金检查   ：买入金额不得超过可用现金
每条检查都记录为 RiskCheck，随决策一起写入审计日志。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from common.events import Action, DecisionEvent, Market, RiskCheck
from common.models import Account
from knowledge_layer.knowledge_service import KnowledgeService


@dataclass
class RiskLimits:
    max_risk_per_trade: float = 0.01      # R1: 单笔 ≤ 净值1%
    max_position_pct: float = 0.20        # R2: 单标的 ≤ 20%
    max_drawdown_halt: float = 0.10       # R3: 回撤 10% 熔断
    event_window_minutes: int = 60        # R4: 事件前降仓窗口
    event_window_scale: float = 0.5       #     窗口内开仓数量缩放
    cooldown_minutes: int = 10            # R5: 同标的冷却期


@dataclass
class RiskState:
    last_trade_at: dict[str, datetime] = field(default_factory=dict)


class RiskManager:
    def __init__(
        self,
        account: Account,
        limits: RiskLimits | None = None,
    ) -> None:
        self.account = account
        self.limits = limits or RiskLimits()
        self.state = RiskState()
        self.knowledge = KnowledgeService()

    # ------------------------------------------------------------ 主入口

    def check(self, decision: DecisionEvent, price: float, calendar_window: bool) -> DecisionEvent:
        """对决策执行风控门禁。原则：SELL（降风险）宽松，BUY（加风险）严格。"""
        checks: list[RiskCheck] = []
        now = datetime.now(timezone.utc)

        if decision.action == Action.HOLD:
            decision.risk_checks.append(RiskCheck("trivial", True, "HOLD 无需风控"))
            return decision

        # R3 回撤熔断（仅限制开仓；平仓放行）
        if decision.action == Action.BUY and self.account.halted:
            checks.append(RiskCheck("R3-熔断", False, "账户已熔断，需人工解锁，禁止开仓"))
            decision.quantity = 0
            decision.blocked_by_risk = True

        equity = self.account.equity({decision.symbol: price})
        notional = decision.quantity * price

        if decision.action == Action.BUY:
            # R1 单笔风险
            if notional > equity * self.limits.max_risk_per_trade:
                # 风控不是否决而是"削减到合规"：把数量砍到限额内
                allowed = equity * self.limits.max_risk_per_trade / price
                step = self.knowledge.market_rule(decision.market).min_qty_step
                allowed = self.knowledge.rulebook.round_qty(decision.market, allowed)
                checks.append(
                    RiskCheck("R1-单笔风险", True,
                              f"原量{decision.quantity:.6g}超限，削减至{allowed:.6g}"))
                decision.quantity = allowed
                decision.blocked_by_risk = decision.quantity <= 0
            else:
                checks.append(RiskCheck("R1-单笔风险", True,
                                        f"{notional:.2f} ≤ {equity * self.limits.max_risk_per_trade:.2f}"))

            # R2 单标的仓位
            pos_value = self.account.position(decision.symbol, decision.market).market_value(price)
            if pos_value + notional > equity * self.limits.max_position_pct:
                room = equity * self.limits.max_position_pct - pos_value
                if room <= 0:
                    checks.append(RiskCheck("R2-标的仓位", False, "仓位已满，拒绝加仓"))
                    decision.quantity = 0
                    decision.blocked_by_risk = True
                else:
                    decision.quantity = self.knowledge.rulebook.round_qty(
                        decision.market, min(decision.quantity, room / price))
                    checks.append(RiskCheck("R2-标的仓位", True, f"削减至{decision.quantity:.6g}"))
            else:
                checks.append(RiskCheck("R2-标的仓位", True, "ok"))

            # R4 事件窗口降仓
            if calendar_window and not decision.blocked_by_risk:
                decision.quantity = self.knowledge.rulebook.round_qty(
                    decision.market, decision.quantity * self.limits.event_window_scale)
                checks.append(RiskCheck("R4-事件窗口", True,
                                        f"重大事件前，数量减半至{decision.quantity:.6g}"))

            # R5 冷却期
            last = self.state.last_trade_at.get(decision.symbol)
            if last and now - last < timedelta(minutes=self.limits.cooldown_minutes):
                checks.append(RiskCheck("R5-冷却期", False, "同标的冷却期内，暂缓开仓"))
                decision.blocked_by_risk = True
            else:
                checks.append(RiskCheck("R5-冷却期", True, "ok"))

            # R6 现金
            if not decision.blocked_by_risk and notional > self.account.cash:
                decision.quantity = self.knowledge.rulebook.round_qty(
                    decision.market, self.account.cash / price)
                checks.append(RiskCheck("R6-现金", True, f"按现金削减至{decision.quantity:.6g}"))
                if decision.quantity <= 0:
                    decision.blocked_by_risk = True
            else:
                checks.append(RiskCheck("R6-现金", True, "ok"))
        else:
            # SELL：检查是否有足够持仓
            pos = self.account.position(decision.symbol, decision.market)
            if decision.quantity > pos.qty:
                decision.quantity = pos.qty
                checks.append(RiskCheck("S1-持仓", True, f"卖出量削减至持仓{pos.qty:.6g}"))
            else:
                checks.append(RiskCheck("S1-持仓", True, "ok"))

        # 数量归零则视为被风控拦截
        if decision.quantity <= 0:
            decision.quantity = 0
            decision.blocked_by_risk = True

        decision.risk_checks = checks
        return decision

    # ------------------------------------------------------------ 状态维护

    def record_trade(self, symbol: str) -> None:
        self.state.last_trade_at[symbol] = datetime.now(timezone.utc)

    def update_drawdown(self, prices: dict[str, float]) -> float:
        """成交回流后调用：更新净值高水位，判断是否触发熔断。"""
        equity = self.account.equity(prices)
        dd = self.account.update_water_mark(equity)
        if dd >= self.limits.max_drawdown_halt and not self.account.halted:
            self.account.halted = True
        return dd

    def unlock(self) -> None:
        """人工解锁熔断（也可在复盘后重置高水位）。"""
        self.account.halted = False
