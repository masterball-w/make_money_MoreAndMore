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
    Candle,
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
from decision_layer.follow_book import FollowBook
from decision_layer.llm_analyzer import LLMAnalyzer, NewsAnalysis
from decision_layer.quant_engine import QuantEngine
from decision_layer.risk_manager import RiskManager
from knowledge_layer import indicators
from knowledge_layer.knowledge_service import KnowledgeService

logger = logging.getLogger("strategy")


def fifteen_minute_trend(
    candles: list[Candle], *, fast: int = 8, slow: int = 21, bar_minutes: int = 15,
) -> float:
    """用已收盘的 1 分钟 K 线合成更高周期趋势。

    快慢 EMA 同向且价格在慢线同一侧才返回 +1 或 -1。样本不够返回 0（不允许开仓）。
    未完成的最后一根高周期 K 线不参与计算。
    """
    if bar_minutes <= 1 or slow <= 1 or not candles:
        return 0.0
    groups: dict[datetime, list[tuple[datetime, float]]] = {}
    for candle in candles:
        ts = candle.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        key = ts.replace(minute=(ts.minute // bar_minutes) * bar_minutes, second=0, microsecond=0)
        groups.setdefault(key, []).append((ts, candle.close))
    closes: list[float] = []
    need = (bar_minutes - 1) * 60
    for key in sorted(groups):
        pts = groups[key]
        if (pts[-1][0] - key).total_seconds() >= need:
            closes.append(pts[-1][1])
    if len(closes) < slow:
        return 0.0
    fast_ema = indicators.ema(closes, fast)
    slow_ema = indicators.ema(closes, slow)
    if fast_ema[-1] is None or slow_ema[-1] is None:
        return 0.0
    price = closes[-1]
    if fast_ema[-1] > slow_ema[-1] and price > slow_ema[-1]:
        return 1.0
    if fast_ema[-1] < slow_ema[-1] and price < slow_ema[-1]:
        return -1.0
    return 0.0


@dataclass
class StrategyConfig:
    # 日内短线权重（2026-10 调参：新闻情绪分钟级滞后明显，降为辅助；
    # 技术突破与资金流异常是 1m 级别的主驱动，LLM 单独不再足以开仓）
    w_llm: float = 0.20
    w_quant: float = 0.25
    w_breakout: float = 0.55         # 技术突破信号权重（满强度可单独触发开仓）
    w_flow: float = 0.50             # 资金流信号权重（大额异常可单独触发）
    entry_threshold: float = 0.35      # |score| 达到该值才开仓
    conflict_threshold: float = 0.4    # 双方强度超过该值且方向相反 → 矛盾
    sentiment_ttl_minutes: int = 90    # 新闻情绪有效期
    signal_ttl_minutes: int = 15       # 突破/资金流信号有效期（线性衰减）
    # 短线持仓纪律：进场后由这三个硬规则管理离场，不再"等反向信号"
    stop_loss_pct: float = 0.012       # 止损线 1.2%（原 5%，日内收紧）
    take_profit_pct: float | None = 0.015   # 止盈落袋 1.5%（None 关闭）
    time_stop_minutes: int | None = 30      # 持仓超时无条件离场（None 关闭）
    min_decision_interval: float = 2.0  # 同标的两次评估最小间隔（秒，演示用小值）
    # 更高周期趋势过滤：只在 15 分钟 EMA 多头时接受做多突破。默认关，避免改演示/单测。
    trend_filter: bool = False
    trend_fast: int = 8
    trend_slow: int = 21
    trend_bar_minutes: int = 15
    # 跟随大额流入。默认关，演示和旧单测不改变行为。
    follow_flow: bool = False
    follow_min_strength: float = 0.45
    # 果断止盈三段式（默认全关，由 risk_limits.yaml 开启）：
    #   浮盈 ≥ breakeven_at → 止损线抬到 入场价+breakeven_lock（保本+覆盖费用）
    #   浮盈 ≥ trail_at → 止损线改为跟踪：最高价回落 trail_gap 即落袋
    #   take_profit_pct 仍为硬顶。三档都用 0 关闭对应档位。
    breakeven_at: float = 0.0
    breakeven_lock: float = 0.0015
    trail_at: float = 0.0
    trail_gap: float = 0.0025
    # 震荡收割通道：布林带极端+RSI 极值逆向（默认关）。专吃区间行情，
    # 与趋势过滤互补：趋势模式追突破，震荡模式高抛低吸。
    mr_enable: bool = False
    mr_rsi_low: float = 30.0           # RSI<此值且价格贴下轨 → 逆向买
    mr_rsi_high: float = 70.0          # RSI>此值且价格贴上轨且有持仓 → 逆向卖
    mr_retry_seconds: float = 120.0    # 逆向单被风控拦后的重试间隔（防审计刷屏）
    # 挂单进场（maker）：费率 0.1%→0.05%，代价是可能不成交（超时撤销）。
    # 只作用于进场买单；所有离场（止损/止盈/跟踪/超时）永远吃单——离场不排队。
    maker_entries: bool = False


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
        self.signal_cache: dict[str, tuple[SignalEvent, datetime]] = {}
        # symbol -> 已消费（已触发过开仓）的信号 id —— 一个信号只操作一次
        self.consumed_signals: dict[str, str] = {}
        self.market_cache: dict[str, MarketEvent] = {}
        self.last_eval_at: dict[str, datetime] = {}
        # symbol -> 持仓开仓时刻（时间止损用；成交回报维护）
        self.position_since: dict[str, datetime] = {}
        # symbol -> 15 分钟趋势方向（+1 多 / -1 空 / 0 不明）
        self.trend_bias: dict[str, float] = {}
        # 逆势突破已记过日志的信号 id，避免每 5 秒刷一条
        self._trend_skip_logged: dict[str, str] = {}
        self.follows = FollowBook()
        self._follow_skip_logged: dict[str, str] = {}
        # symbol -> 持仓期间最高价（跟踪止盈用）
        self.pos_high_water: dict[str, float] = {}
        # symbol -> 上次尝试震荡收割的时刻（防被风控拦截后每分钟刷审计）
        self._mr_last_try: dict[str, datetime] = {}
        # 回测注入事件时钟（None=墙钟）；TTL/超时/限流全部走 _now()
        self._clock_override: datetime | None = None
        self._stopped = False

    def _now(self) -> datetime:
        return self._clock_override or datetime.now(timezone.utc)

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
        now = self._now()
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
        self.trend_bias[event.symbol] = fifteen_minute_trend(
            event.history, fast=self.cfg.trend_fast, slow=self.cfg.trend_slow,
            bar_minutes=self.cfg.trend_bar_minutes)
        self.audit.log_kline(event)          # K 线落盘（监控面板实时 K 线图）
        await self._evaluate(event.symbol)

    async def on_signal(self, event: SignalEvent) -> None:
        """指标突破信号 → 缓存（带 TTL）→ 立即强制评估（技术突破驱动操作）。"""
        now = self._now()
        self.signal_cache[event.symbol] = (event, now)
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
        # 时间止损的锚点：首次建仓时刻；清仓后移除（加仓不重置）。
        # 灰尘仓位（< 最小交易单位，源于基础币手续费）直接清零。
        if pos.qty <= 1e-12:
            self.position_since.pop(event.symbol, None)
            self.pos_high_water.pop(event.symbol, None)
        else:
            self.position_since.setdefault(event.symbol, self._now())
            # 跟踪止盈锚点：持仓期间最高价（加仓不重置）
            self.pos_high_water[event.symbol] = max(
                self.pos_high_water.get(event.symbol, event.avg_price), event.avg_price)
        step = self.knowledge.market_rule(event.market).min_qty_step
        if 1e-12 < pos.qty < step:
            dust_qty, dust_value = pos.qty, pos.qty * event.avg_price
            pos.qty = 0.0
            logger.info("[决策层] 清除灰尘仓位 %s（%.8g，约 %.2f USDT）",
                        event.symbol, dust_qty, dust_value)
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
        now = self._now()
        last = self.last_eval_at.get(symbol)
        if not force and last and (now - last).total_seconds() < self.cfg.min_decision_interval:
            return
        self.last_eval_at[symbol] = now

        price = market_ev.price
        account = self.risk.account
        pos = account.position(symbol, market_ev.market)

        # 持仓判定用最小交易单位过滤"灰尘仓位"（买入手续费按基础币扣减
        # 会留下 <0.0001 的残渣，不过滤会导致止损单被舍入为0而无限重复拦截）
        step = self.knowledge.market_rule(market_ev.market).min_qty_step
        has_pos = pos.qty >= step

        # 1) 持仓管理优先（短线纪律，不依赖任何信号）：
        #    动态止损线（固定止损 → 保本锁定 → 跟踪止盈）→ 硬止盈 → 超时离场
        if has_pos:
            stop = self.knowledge.risk.stop_loss_price(pos.avg_price, self.cfg.stop_loss_pct)
            exit_kind = "止损触发"
            unreal = (price / pos.avg_price - 1) if pos.avg_price else 0.0
            hw = max(self.pos_high_water.setdefault(symbol, pos.avg_price), price)
            self.pos_high_water[symbol] = hw
            if self.cfg.breakeven_at and unreal >= self.cfg.breakeven_at:
                be = pos.avg_price * (1 + self.cfg.breakeven_lock)
                if be > stop:
                    stop, exit_kind = be, "保本离场"
            if self.cfg.trail_at and unreal >= self.cfg.trail_at:
                trail_stop = hw * (1 - self.cfg.trail_gap)
                if trail_stop > stop:
                    stop, exit_kind = trail_stop, "跟踪止盈落袋"
            if price <= stop:
                leader = self.follows.leader.get(symbol, "")
                extra = f" 停止跟随{leader}" if leader else ""
                ok = await self._emit(
                    Action.SELL, symbol, market_ev, pos.qty, 1.0,
                    reason=f"{exit_kind}: 现价{price:.2f} ≤ 离场线{stop:.2f}{extra}",
                    trigger_news=trigger_news)
                if ok:
                    self._end_follow(symbol, failed=exit_kind == "止损触发")
                return
            if self.cfg.take_profit_pct:
                tp = pos.avg_price * (1 + self.cfg.take_profit_pct)
                if price >= tp:
                    ok = await self._emit(
                        Action.SELL, symbol, market_ev, pos.qty, 1.0,
                        reason=f"止盈落袋: 现价{price:.2f} ≥ 目标{tp:.2f}",
                        trigger_news=trigger_news)
                    if ok:
                        self._end_follow(symbol, failed=False)
                    return
            if self.cfg.time_stop_minutes:
                since = self.position_since.get(symbol)
                if since is not None:
                    held_min = (self._now() - since).total_seconds() / 60
                    if held_min >= self.cfg.time_stop_minutes:
                        ok = await self._emit(
                            Action.SELL, symbol, market_ev, pos.qty, 0.6,
                            reason=f"超时离场: 持仓{held_min:.0f}分钟未达预期，收回资金",
                            trigger_news=trigger_news)
                        if ok:
                            self._end_follow(symbol, failed=False)
                        return

        # 1.5) 震荡收割通道（默认关）：下轨超卖接回落 / 上轨超买卖兑现。
        #     与突破通道互补——区间行情里突破追高挨打，这条专吃回归。
        if self.cfg.mr_enable and len(market_ev.history) >= 30:
            hist = market_ev.history
            rsi_series = indicators.rsi(hist, 14)
            mid_s, up_s, lo_s = indicators.bollinger(hist, 20, 2.0)
            rsi = rsi_series[-1] if rsi_series and rsi_series[-1] is not None else None
            boll_up = up_s[-1] if up_s and up_s[-1] is not None else None
            boll_lo = lo_s[-1] if lo_s and lo_s[-1] is not None else None
            if rsi is not None and boll_up is not None and boll_lo is not None:
                last_try = self._mr_last_try.get(symbol)
                throttled = bool(last_try and
                                 (self._now() - last_try).total_seconds() < self.cfg.mr_retry_seconds)
                if not throttled:
                    if has_pos and rsi > self.cfg.mr_rsi_high and price >= boll_up:
                        self._mr_last_try[symbol] = self._now()
                        ok = await self._emit(
                            Action.SELL, symbol, market_ev, pos.qty, 0.8,
                            reason=f"震荡收割: RSI{rsi:.0f}超买+触及上轨，逆势兑现",
                            trigger_news=trigger_news)
                        if ok:
                            self._end_follow(symbol, failed=False)
                        return
                    if not has_pos and rsi < self.cfg.mr_rsi_low and price <= boll_lo:
                        self._mr_last_try[symbol] = self._now()
                        notional = account.equity({symbol: price}) * self.risk.limits.max_risk_per_trade
                        ok = await self._emit(
                            Action.BUY, symbol, market_ev, notional / price, 0.6,
                            reason=f"震荡收割: RSI{rsi:.0f}超卖+触及下轨，接回落",
                            trigger_news=trigger_news, maker=self.cfg.maker_entries)
                        return

        # 2) 三路信号融合：LLM 情绪 + 量化常规信号 + 技术突破信号
        quant_sig = self.quant.signal(market_ev)
        cached = self._valid_sentiment(symbol)
        llm_sent, llm_analysis = (cached[0].sentiment, cached[0]) if cached else (None, None)
        sig = self._valid_signal(symbol)
        ev_sig = sig[0] if sig else None

        if quant_sig is None and llm_sent is None and ev_sig is None:
            return  # 无任何信号输入

        q_val = quant_sig.value if quant_sig else 0.0
        l_val = llm_sent if llm_sent is not None else 0.0
        conf = llm_analysis.confidence if llm_analysis else 0.5
        ev_term = 0.0
        if ev_sig is not None and self.consumed_signals.get(symbol) != ev_sig.id:
            age = (now - sig[1]).total_seconds() / 60.0
            decay = max(0.0, 1.0 - age / self.cfg.signal_ttl_minutes)   # TTL 线性衰减
            # 事件信号权重按类型分派：资金流异常 vs 技术突破
            w_ev = self.cfg.w_flow if ev_sig.kind.startswith("flow_") else self.cfg.w_breakout
            ev_term = w_ev * ev_sig.direction * ev_sig.strength * decay

        # 矛盾检测：LLM 与量化方向相反且都强 → 保守观望。
        # 豁免：事件信号（资金流/突破）与 LLM 同向且足够强时视为"多路确认"，
        # 量化技术指标滞后不构成否决依据（EMA 等趋势指标在急变行情中天然滞后）。
        if llm_sent is not None and quant_sig is not None:
            confirmed_by_event = (ev_sig is not None and ev_sig.direction * l_val > 0
                                  and ev_sig.strength >= 0.3)
            if (l_val * q_val < 0 and abs(l_val) >= self.cfg.conflict_threshold
                    and abs(q_val) >= self.cfg.conflict_threshold and not confirmed_by_event):
                await self._emit(
                    Action.HOLD, symbol, market_ev, 0, 0.3,
                    reason=f"信号矛盾: LLM{l_val:+.2f} vs 量化{q_val:+.2f}，保守观望",
                    trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                    llm_reasoning=llm_analysis.reasoning if llm_analysis else "")
                return

        score = (self.cfg.w_llm * l_val * (0.5 + conf / 2)
                 + self.cfg.w_quant * q_val
                 + ev_term)
        sig_desc = (f"{ev_sig.kind}({ev_sig.direction:+.0f}x{ev_sig.strength:.2f})"
                    if ev_sig else "无事件信号")
        entry = self.cfg.entry_threshold

        trend = self.trend_bias.get(symbol, 0.0)
        if score >= entry and self.cfg.trend_filter and trend <= 0:
            # 方向相反的信号互相抵消，不进场。边沿消失前持仓仍由止损/止盈/反向信号处理。
            sig_id = ev_sig.id if ev_sig is not None else ""
            if sig_id and self._trend_skip_logged.get(symbol) != sig_id:
                self._trend_skip_logged[symbol] = sig_id
                logger.info("[决策层] 逆势不做 %s score=%+.2f 15m趋势=%+.0f | %s",
                            symbol, score, trend, sig_desc)
            return

        if score >= entry and self.cfg.follow_flow:
            kind = ev_sig.kind if ev_sig is not None else ""
            strength = ev_sig.strength if ev_sig is not None else 0.0
            leader_id = ev_sig.id if ev_sig is not None else None
            if not self.follows.can_enter(
                    symbol, leader_id, kind, strength, self.cfg.follow_min_strength):
                skip_key = leader_id or "wait"
                if self._follow_skip_logged.get(symbol) != skip_key:
                    self._follow_skip_logged[symbol] = skip_key
                    logger.info("[跟随] 不跟 %s（已止损或在等下一笔大额流入） score=%+.2f",
                                skip_key, score)
                return

        if score >= entry:
            notional = account.equity({symbol: price}) * self.risk.limits.max_risk_per_trade
            qty = notional / price
            follow_note = ""
            if (self.cfg.follow_flow and ev_sig is not None and ev_sig.kind == "flow_in"
                    and ev_sig.strength >= self.cfg.follow_min_strength):
                follow_note = f" 跟随{ev_sig.id}"
            ok = await self._emit(
                Action.BUY, symbol, market_ev, qty, min(1.0, abs(score) * 1.5),
                reason=f"信号看多 score={score:+.2f} (LLM{l_val:+.2f}xconf{conf:.1f} "
                       f"+ 量化{q_val:+.2f}"
                       + (f" [{quant_sig.detail}]" if quant_sig else "")
                       + f" + {sig_desc}){follow_note}",
                trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                llm_reasoning=llm_analysis.reasoning if llm_analysis else "",
                maker=self.cfg.maker_entries)
            if ok and ev_sig is not None:
                # 一个事件信号只消费一次：已据此开仓，TTL 内不再重复加仓
                self.consumed_signals[symbol] = ev_sig.id
                self._begin_follow(symbol, ev_sig)
        elif score <= -entry and has_pos:
            ok = await self._emit(
                Action.SELL, symbol, market_ev, pos.qty, min(1.0, abs(score) * 1.5),
                reason=f"信号看空 score={score:+.2f} (LLM{l_val:+.2f} + 量化{q_val:+.2f} + {sig_desc})，清仓离场",
                trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                llm_reasoning=llm_analysis.reasoning if llm_analysis else "")
            if ok:
                self._end_follow(symbol, failed=False)
        else:
            if trigger_news is not None or ev_sig is not None:  # 新闻/事件信号触发的 HOLD 才记录
                await self._emit(
                    Action.HOLD, symbol, market_ev, 0, min(1.0, abs(score)),
                    reason=f"信号不足 score={score:+.2f}，按兵不动",
                    trigger_news=trigger_news, quant_signal=q_val, llm_sentiment=l_val,
                    llm_reasoning=llm_analysis.reasoning if llm_analysis else "")

    def _valid_signal(self, symbol: str) -> tuple[SignalEvent, datetime] | None:
        cached = self.signal_cache.get(symbol)
        if not cached:
            return None
        _, received_at = cached
        if self._now() - received_at > timedelta(minutes=self.cfg.signal_ttl_minutes):
            return None
        return cached

    def _valid_sentiment(self, symbol: str):
        cached = self.sentiment_cache.get(symbol)
        if not cached:
            return None
        analysis, received_at, _ = cached
        ttl = timedelta(minutes=self.cfg.sentiment_ttl_minutes)
        if self._now() - received_at > ttl:
            return None
        return cached

    def _begin_follow(self, symbol: str, ev_sig: SignalEvent) -> None:
        if not self.cfg.follow_flow or ev_sig.kind != "flow_in":
            return
        if ev_sig.strength < self.cfg.follow_min_strength:
            return
        if ev_sig.id in self.follows.blocked.get(symbol, set()):
            return
        self.follows.begin(symbol, ev_sig.id)
        self._follow_skip_logged.pop(symbol, None)
        logger.info("[跟随] 跟随大额流入 %s 强度%.2f", ev_sig.id, ev_sig.strength)

    def _end_follow(self, symbol: str, *, failed: bool) -> None:
        if not self.cfg.follow_flow:
            return
        leader = self.follows.finish(symbol, failed=failed)
        if not leader:
            return
        if failed:
            logger.info("[跟随] 止损，停止跟随 %s，等待下一笔大额流入", leader)
        else:
            logger.info("[跟随] 结束跟随 %s", leader)

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
        maker: bool = False,
        quant_signal: float | None = None,
        llm_sentiment: float | None = None,
        llm_reasoning: str = "",
    ) -> bool:
        """产出决策；返回是否真实发起了下单（True）或仅审计（False）。"""
        qty = self.knowledge.rulebook.round_qty(market_ev.market, qty)
        decision = DecisionEvent(
            ts=self._now(),               # 事件时钟（回测=事件时间，实盘=墙钟）
            action=action,
            symbol=symbol,
            market=market_ev.market,
            quantity=qty,
            reference_price=market_ev.price,
            maker=maker and action == Action.BUY,   # 只挂进场买单
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
