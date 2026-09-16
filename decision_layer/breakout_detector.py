"""指标突破检测器 —— 对实时 K 线测量技术指标，穿越阈值即产出 SignalEvent。

检测的突破类型（全部趋势跟踪语义，方向一致）：
  macd_golden / macd_death : DIF 上穿/下穿 DEA（金叉/死叉）
  boll_up   / boll_down    : 收盘价上穿布林上轨 / 下穿布林下轨
  donch_up  / donch_down   : 突破/跌破前 N 根已收盘 K 线的最高/最低价（海龟入场）

触发条件是"穿越"而非"处于"：prev 在阈值一侧、curr 在另一侧才触发，
避免价格持续贴着轨道时重复报信号。

去抖：同一根 K 线（同一 ts）同一类突破只报一次；同一次更新若多个突破
同时发生则合并为一个 SignalEvent（方向冲突时取强度最大者）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from common.bus import EventBus
from common.events import KlineEvent, SignalEvent
from knowledge_layer import indicators

logger = logging.getLogger("breakout")


@dataclass
class BreakoutConfig:
    donchian_period: int = 20
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    min_candles: int = 35            # 指标预热所需最少 K 线数
    atr_period: int = 14
    min_strength: float = 0.15       # 低于该强度的突破不报（噪声过滤）


@dataclass
class _IndicatorState:
    """上一 tick 的指标快照 —— 穿越检测的比较基准。"""

    close: float | None = None
    dif: float | None = None
    dea: float | None = None
    upper: float | None = None
    lower: float | None = None
    don_high: float | None = None
    don_low: float | None = None
    fired: set[tuple[str, float]] = field(default_factory=set)   # (kind, candle_ts)


class BreakoutDetector:
    def __init__(self, bus: EventBus, config: BreakoutConfig | None = None) -> None:
        self.bus = bus
        self.cfg = config or BreakoutConfig()
        self.state: dict[str, _IndicatorState] = {}

    def bind(self) -> None:
        self.bus.subscribe("kline", self.on_kline)
        logger.info("突破检测器已订阅 kline（MACD/布林/Donchian %d 根）",
                    self.cfg.donchian_period)

    # ------------------------------------------------------------ 主流程

    async def on_kline(self, ev: KlineEvent) -> None:
        st = self.state.setdefault(ev.symbol, _IndicatorState())
        candles = list(ev.history) + ([ev.current] if ev.current else [])
        if len(candles) < self.cfg.min_candles:
            return

        closes = [c.close for c in candles]
        close = closes[-1]

        # Donchian 通道："当前根"=正在形成的 K 线；若恰好都已收盘，
        # 则最后一根已收盘 K 线扮演当前根（其自身不计入通道，否则永远无法突破自身高点）
        if ev.current is not None:
            ref_history = ev.history
        else:
            ref_history = ev.history[:-1]
        n = self.cfg.donchian_period
        don_window = ref_history[-n:] if len(ref_history) >= n else ref_history
        don_high = max(c.high for c in don_window) if don_window else None
        don_low = min(c.low for c in don_window) if don_window else None

        # MACD 与布林带：含当前最新价
        dif_series, dea_series, _ = indicators.macd(
            closes, self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal)
        _, upper_series, lower_series = indicators.bollinger(
            closes, self.cfg.bollinger_period, self.cfg.bollinger_std)
        dif, dea = dif_series[-1], dea_series[-1]
        upper, lower = upper_series[-1], lower_series[-1]

        # ATR 用于突破强度归一化
        atr_series = indicators.atr(
            [c.high for c in candles], [c.low for c in candles], closes, self.cfg.atr_period)
        atr = atr_series[-1] or (close * 0.002)   # ATR 缺失时用 0.2% 兜底

        candle_ts = candles[-1].ts.timestamp()
        breakouts: list[SignalEvent] = []

        def _fire(kind: str, direction: float, indicator: float, threshold: float,
                  excess: float, desc: str) -> None:
            if (kind, candle_ts) in st.fired:
                return
            strength = _clamp(abs(excess) / atr, 0.0, 1.0) if atr > 0 else 0.0
            if strength < self.cfg.min_strength:
                return
            st.fired.add((kind, candle_ts))
            breakouts.append(SignalEvent(
                symbol=ev.symbol, market=ev.market, kind=kind, direction=direction,
                strength=round(strength, 4), detail=desc,
                indicator=round(float(indicator), 4), threshold=round(float(threshold), 4),
                interval=ev.interval))

        # 1) MACD 金叉/死叉（需要上一 tick 的 dif/dea 与当前都在）
        if dif is not None and dea is not None and st.dif is not None and st.dea is not None:
            if st.dif <= st.dea and dif > dea:
                _fire("macd_golden", +1, dif, dea, dif - dea,
                      f"MACD金叉: DIF上穿DEA ({dif:.2f}>{dea:.2f})")
            elif st.dif >= st.dea and dif < dea:
                _fire("macd_death", -1, dif, dea, dea - dif,
                      f"MACD死叉: DIF下穿DEA ({dif:.2f}<{dea:.2f})")

        # 2) 布林带上/下轨穿越
        if upper is not None and st.upper is not None and st.close is not None:
            if st.close <= st.upper and close > upper:
                _fire("boll_up", +1, close, upper, close - upper,
                      f"突破布林上轨: {close:.2f} > {upper:.2f}")
        if lower is not None and st.lower is not None and st.close is not None:
            if st.close >= st.lower and close < lower:
                _fire("boll_down", -1, close, lower, lower - close,
                      f"跌破布林下轨: {close:.2f} < {lower:.2f}")

        # 3) Donchian 高低点突破（与上一 tick 的通道值比较）
        if don_high is not None and st.don_high is not None:
            if close > don_high >= st.close:
                _fire("donch_up", +1, close, don_high, close - don_high,
                      f"突破{n}根K线高点: {close:.2f} > {don_high:.2f}")
        if don_low is not None and st.don_low is not None:
            if close < don_low <= st.close:
                _fire("donch_down", -1, close, don_low, don_low - close,
                      f"跌破{n}根K线低点: {close:.2f} < {don_low:.2f}")

        # 更新快照（穿越检测基准）
        st.close, st.dif, st.dea = close, dif, dea
        st.upper, st.lower, st.don_high, st.don_low = upper, lower, don_high, don_low

        if not breakouts:
            return
        merged = self._merge(ev, breakouts)
        logger.info("[突破检测] %s %s 强度%.2f | %s",
                    merged.kind, ev.symbol, merged.strength, merged.detail)
        await self.bus.publish(merged)

    # ------------------------------------------------------------ 合并

    @staticmethod
    def _merge(ev: KlineEvent, signals: list[SignalEvent]) -> SignalEvent:
        """同一次多个突破 → 合并为一个事件；方向冲突取强度最大者。"""
        if len(signals) == 1:
            return signals[0]
        directions = {s.direction for s in signals}
        strongest = max(signals, key=lambda s: s.strength)
        if len(directions) == 1:
            detail = " + ".join(s.detail for s in signals)
            return SignalEvent(
                symbol=ev.symbol, market=ev.market,
                kind="+".join(s.kind for s in signals), direction=strongest.direction,
                strength=strongest.strength, detail=detail,
                indicator=strongest.indicator, threshold=strongest.threshold,
                interval=ev.interval)
        # 方向冲突（罕见）：取强者，detail 说明冲突
        weaker = [s for s in signals if s is not strongest]
        detail = strongest.detail + f"（与之冲突被压制: {'; '.join(s.kind for s in weaker)}）"
        return SignalEvent(
            symbol=ev.symbol, market=ev.market, kind=strongest.kind,
            direction=strongest.direction, strength=strongest.strength, detail=detail,
            indicator=strongest.indicator, threshold=strongest.threshold,
            interval=ev.interval)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
