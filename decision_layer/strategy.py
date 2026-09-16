"""策略编排器 —— 决策层的心脏。

数据流：
    on_news   → LLM 分析 → 情绪缓存（带 TTL）→ P1 强情绪立即触发评估
    on_kline  → 实时 K 线流 → 同步行情缓存（量化引擎输入）→ 常规评估
    on_signal → 指标突破信号（MACD/布林/Donchian 穿越阈值）→ 立即强制评估
    _evaluate → LLM情绪 x 量化信号 x 技术突破 融合 → 决策雏形 → 风控门禁 → 审计 → 发布
    on_fill   → 更新本地持仓/净值 → 回撤检查（熔断）→ 审计成交

融合规则（可配置）：
    score = w_llm*llm_sentiment + w_quant*quant_signal + w_breakout*突破方向*强度
    - LLM 与量化方向相反且都强（|各|>=0.4）→ 矛盾 → HOLD（保守优先）
    - score >= +entry_threshold → BUY；score <= -entry_threshold 且有持仓 → SELL
    止损：持仓浮亏超过 stop_loss_pct 无条件 SELL（不依赖任何信号）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from common.bus import EventBus
from common.events import (
    Action,
    DecisionEvent,
    Event,
    FillEvent,
    KlineEvent,
    MarketEvent,
    NewsEvent,
    Priority,
    SignalEvent,
)
from decision_layer.event_calendar import EconomicCalendar
from decision_layer.llm_analyzer import LLMAnalyzer, NewsAnalysis
from decision_layer.quant_engine import QuantEngine
from decision_layer.risk_manager import RiskManager
from knowledge_layer.knowledge_service import KnowledgeService

logger = logging.getLogger("strategy")


@dataclass
class StrategyConfig:
    w_llm: float = 0.45
    w_quant: float = 0.25
    w_breakout: float = 0.45         # 技术突破信号权重（K线指标穿越触发）
    entry_threshold: float = 0.35      # |score| 达到该值才开仓
    conflict_threshold: float = 0.4    # 双方强度超过该值且方向相反 → 矛盾
    sentiment_ttl_minutes: int = 90    # 新闻情绪有效期
    breakout_ttl_minutes: int = 15     # 突破信号有效期（过期衰减为 0）
    stop_loss_pct: float = 0.05        # 止损线 5%
    min_decision_interval: float = 2.0  # 同标的两次评估最小间隔（秒，演示用小值）


class StrategyOrchestrator:
    def __init__(
        self,
        bus: EventBus,
        analyzer: LLMAnalyzer,
        quant: QuantEngine,
        risk_manager: RiskManager,
        calendar: EconomicCalendar,
        audit,  # AuditLog（避免循环导入用鸭子类型）
        config: StrategyConfig | None = None,
    ) -> None:
        self.bus = bus
        self.analyzer = analyzer
        self.quant = quant
        self.risk = risk_manager
        self.calendar = calendar
        self.audit = audit
        self.cfg = config or StrategyConfig()
        self.knowledge = KnowledgeService()
        # symbol -> (analysis, received_at, news_id)
        self.sentiment_cache: dict[str, tuple[NewsAnalysis, datetime, str]] = {}
        # symbol -> (signal, received_at)
        self.breakout_cache: dict[str, tuple[SignalEvent, datetime]] = {}
        # symbol -> 已消费（已触发过开仓）的突破信号 id —— 一个信号只操作一次
        self.consumed_breakouts: dict[str, str] = {}
        self.market_cache: dict[str, MarketEvent] = {}
        self.last_eval_at: dict[str, datetime] = {}
        self._stopped = False

    # ------------------------------------------------------------ 订阅绑定

    def bind(self) -> None:
        self.bus.subscribe("news", self.on_news)
        self.bus.subscribe("market", self.on_market)
        self.bus.subscribe("kline", self.on_kline)
        self.bus.subscribe("signal", self.on_signal)
        self.bus.subscribe("fill", self.on_fill)
        logger.info("决策层已订阅 news/market/kline/signal/fill")

    # ------------------------------------------------------------ 事件处理

    async def on_news(self, event: NewsEvent) -> None:
        analysis = await self.analyzer.analyze(event)
        now = datetime.now(timezone.utc)
        for symbol in analysis.symbols:
            self.sentiment_cache[symbol] = (analysis, now, event.id)
        logger.info("[决策层] LLM分析(%s): sentiment=%+.2f conf=%.2f | %s",
                    analysis.engine, analysis.sentiment, analysis.confidence,
                    analysis.reasoning[:80])
        # P1 + 强情绪 → 立即评估对应标的（新闻驱动的快速通道，不受评估限流约束）
        strong = abs(analysis.sentiment) >= self.cfg.entry_threshold
        if event.priority == Priority.P1 and strong:
            for symbol in analysis.symbols:
                await self._evaluate(symbol, trigger_news=event, force=True)

    async def on_market(self, event: MarketEvent) -> None:
        self.market_cache[event.symbol] = event
        await self._evaluate(event.symbol)

    async def on_kline(self, event: KlineEvent) -> None:
        """实时 K 线流 → 同步为行情缓存（量化引擎的输入）→ 常规评估。"""
        closes = event.closes()
        if not closes:
            return
        prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
        snap = MarketEvent(
            symbol=event.symbol, market=event.market, price=closes[-1],
            change_pct=(closes[-1] - prev_close) / prev_close if prev_close else 0.0,
            volume=(event.current.volume if event.current else 0.0),
            history=closes[-120:])
        self.market_cache[event.symbol] = snap
        self.audit.log_kline(event)          # K 线落盘（监控面板实时 K 线图）
        await self._evaluate(event.symbol)

    async def on_signal(self, event: SignalEvent) -> None:
        """指标突破信号 → 缓存（带 TTL）→ 立即强制评估（技术突破驱动操作）。"""
        now = datetime.now(timezone.utc)
        self.breakout_cache[event.symbol] = (event, now)
        logger.info("[决策层] 收到突破信号: %s %s 方向%+.0f 强度%.2f",
                    event.kind, event.symbol, event.direction, event.strength)
        await self._evaluate(event.symbol, force=True)

    async def on_fill(self, event: FillEvent) -> None:
        pos = self.risk.account.position(event.symbol, event.market)
        pos.apply_fill(event.side, event.filled_qty, event.avg_price)
        if event.side == Action.BUY:
            self.risk.account.cash -= event.filled_qty * event.avg_price + event.commission
        else:
            self.risk.account.cash += event.filled_qty * event.avg_price - event.commission
        self.audit.log_fill(event)
        prices = {s: m.price for s, m in self.market_cache.items()}
        dd = self.risk.update_drawdown(prices)
        if self.risk.account.halted:
            logger.error("[风控熔断] 回撤 %.1f%% 达到上限，停止一切开仓，等待人工解锁", dd * 100)
        logger.info("[决策层] 成交回流: %s %s %.6g @ %.2f | 净值=%.2f 回撤=%.2f%%",
                    event.side.value.upper(), event.symbol, event.filled_qty,
                    event.avg_price, self.risk.account.equity(prices), dd * 100)

    # ------------------------------------------------------------ 决策评估

    async def _evaluate(self, symbol: str, trigger_news: NewsEvent | None = None,
                        force: bool = False) -> None:
        market_ev = self.market_cache.get(symbol)
        if market_ev is None:
            return
        # 最小评估间隔（防止高频行情触发决策风暴）；P1 紧急新闻 force=True 绕过
        now = datetime.now(timezone.utc)
        last = self.last_eval_at.get(symbol)
        if not force and last and (now - last).total_seconds() < self.cfg.min_decision_interval:
            return
        self.last_eval_at[symbol] = now

        price = market_ev.price
        account = self.risk.account
        pos = account.position(symbol, market_ev.market)

        # 1) 止损优先：浮亏超线无条件平仓
        if pos.qty > 0:
            stop = self.knowledge.risk.stop_loss_price(pos.avg_price, self.cfg.stop_loss_pct)
            if price <= stop:
                await self._emit(
                    Action.SELL, symbol, market_ev, pos.qty, 1.0,
                    reason=f"止损触发: 现价{price:.2f} ≤ 止损线{stop:.2f}",
                    trigger_news=trigger_news)
                return

        # 2) 三路信号融合：LLM 情绪 + 量化常规信号 + 技术突破信号
        quant_sig = self.quant.signal(market_ev)
        cached = self._valid_sentiment(symbol)
        llm_sent, llm_analysis = (cached[0].sentiment, cached[0]) if cached else (None, None)
        bk = self._valid_breakout(symbol)
        bk_sig = bk[0] if bk else None

        if quant_sig is None and llm_sent is None and bk_sig is None:
            return  # 无任何信号输入

        q_val = quant_sig.value if quant_sig else 0.0
        l_val = llm_sent if llm_sent is not None else 0.0
        conf = llm_analysis.confidence if llm_analysis else 0.5
        bk_term = 0.0
        if bk_sig is not None and self.consumed_breakouts.get(symbol) != bk_sig.id:
            age = (now - bk[1]).total_seconds() / 60.0
            decay = max(0.0, 1.0 - age / self.cfg.breakout_ttl_minutes)   # TTL 线性衰减
            bk_term = bk_sig.direction * bk_sig.strength * decay

        # 矛盾检测：LLM 与量化方向相反且都强 → 保守观望
        if llm_sent is not None and quant_sig is not None:
            if l_val * q_val < 0 and abs(l_val) >= self.cfg.conflict_threshold and abs(q_val) >= self.cfg.conflict_threshold:
                await self._emit(
                    Action.HOLD, symbol, market_ev, 0, 0.3,
                    reason=f"信号矛盾: LLM{l_val:+.2f} vs 量化{q_val:+.2f}，保守观望",
                    trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                    llm_reasoning=llm_analysis.reasoning if llm_analysis else "")
                return

        score = (self.cfg.w_llm * l_val * (0.5 + conf / 2)
                 + self.cfg.w_quant * q_val
                 + self.cfg.w_breakout * bk_term)
        bk_desc = f"突破{bk_sig.kind}({bk_sig.direction:+.0f}x{bk_sig.strength:.2f})" if bk_sig else "无突破"
        entry = self.cfg.entry_threshold

        if score >= entry:
            notional = account.equity({symbol: price}) * self.risk.limits.max_risk_per_trade
            qty = notional / price
            ok = await self._emit(
                Action.BUY, symbol, market_ev, qty, min(1.0, abs(score) * 1.5),
                reason=f"信号看多 score={score:+.2f} (LLM{l_val:+.2f}xconf{conf:.1f} "
                       f"+ 量化{q_val:+.2f}"
                       + (f" [{quant_sig.detail}]" if quant_sig else "")
                       + f" + {bk_desc})",
                trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                llm_reasoning=llm_analysis.reasoning if llm_analysis else "")
            if ok and bk_sig is not None:
                # 一个突破信号只消费一次：已据此开仓，TTL 内不再重复加仓
                self.consumed_breakouts[symbol] = bk_sig.id
        elif score <= -entry and pos.qty > 0:
            await self._emit(
                Action.SELL, symbol, market_ev, pos.qty, min(1.0, abs(score) * 1.5),
                reason=f"信号看空 score={score:+.2f} (LLM{l_val:+.2f} + 量化{q_val:+.2f} + {bk_desc})，清仓离场",
                trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                llm_reasoning=llm_analysis.reasoning if llm_analysis else "")
        else:
            if trigger_news is not None or bk_sig is not None:  # 新闻/突破触发的 HOLD 才记录
                await self._emit(
                    Action.HOLD, symbol, market_ev, 0, min(1.0, abs(score)),
                    reason=f"信号不足 score={score:+.2f}，按兵不动",
                    trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                    llm_reasoning=llm_analysis.reasoning if llm_analysis else "")

    def _valid_breakout(self, symbol: str) -> tuple[SignalEvent, datetime] | None:
        cached = self.breakout_cache.get(symbol)
        if not cached:
            return None
        _, received_at = cached
        if datetime.now(timezone.utc) - received_at > timedelta(minutes=self.cfg.breakout_ttl_minutes):
            return None
        return cached

    def _valid_sentiment(self, symbol: str):
        cached = self.sentiment_cache.get(symbol)
        if not cached:
            return None
        analysis, received_at, _ = cached
        ttl = timedelta(minutes=self.cfg.sentiment_ttl_minutes)
        if datetime.now(timezone.utc) - received_at > ttl:
            return None
        return cached

    # ------------------------------------------------------------ 产出决策

    async def _emit(
        self,
        action: Action,
        symbol: str,
        market_ev: MarketEvent,
        qty: float,
        confidence: float,
        reason: str,
        trigger_news: NewsEvent | None = None,
        quant_signal: float | None = None,
        llm_sentiment: float | None = None,
        llm_reasoning: str = "",
    ) -> bool:
        """产出决策；返回是否真实发起了下单（True）或仅审计（False）。"""
        qty = self.knowledge.rulebook.round_qty(market_ev.market, qty)
        decision = DecisionEvent(
            action=action,
            symbol=symbol,
            market=market_ev.market,
            quantity=qty,
            reference_price=market_ev.price,
            confidence=max(0.0, min(1.0, confidence)),
            reason=reason,
            llm_sentiment=llm_sentiment,
            llm_reasoning=llm_reasoning,
            quant_signal=quant_signal,
            news_ids=[trigger_news.id] if trigger_news else [],
        )
        # 风控门禁（所有决策必经）
        window = self.calendar.active_window() is not None
        decision = self.risk.check(decision, market_ev.price, calendar_window=window)
        if window:
            logger.warning("[决策层] 处于重大事件前窗口，已自动降仓")

        self.audit.log_decision(decision)
        passed = [c for c in decision.risk_checks if not c.passed]
        if decision.blocked_by_risk or decision.action == Action.HOLD:
            logger.info("[决策层] %s %s qty=%.6g | %s | 风控拦截:%s",
                        action.value.upper(), symbol, decision.quantity, reason[:60],
                        ";".join(c.name for c in passed) or "无")
            return False  # 被拦截/HOLD：只审计，不下单

        self.risk.record_trade(symbol)
        logger.info("[决策层] >>> %s %s qty=%.6g @~%.2f | %s",
                    action.value.upper(), symbol, decision.quantity,
                    decision.reference_price, reason[:70])
        await self.bus.publish(decision)
        return True
