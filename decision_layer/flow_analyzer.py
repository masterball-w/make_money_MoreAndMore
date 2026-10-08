"""资金流分析器 —— 检测"大量流入/大额抛售"并产出 SignalEvent。

检测逻辑（三个维度，全部可解释）：
  F1 方向异常：量能失衡度 imbalance 的滚动 z-score 超过阈值
     → flow_in（买方异常激进，资金大量流入）/ flow_out（抛售）
  F2 量能异常：总成交量 z-score > 2（放量）时信号强度增强
     —— "大量"流入 = 方向异常 × 放量，两个维度同时确认
  F3 量价背离：资金大额流入但价格下跌（或反向）→ 强度减半并标注
     —— 背离常见于大资金对倒出货，是经典陷阱形态

去抖：同方向信号在 cooldown 期内不重复发布。
产出复用 SignalEvent 通道（kind=flow_in/flow_out），与突破信号同一融合入口。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from common.bus import EventBus
from common.events import FlowEvent, KlineEvent, SignalEvent
from knowledge_layer.formulas import flow as flow_math

logger = logging.getLogger("flow_analyzer")


@dataclass
class FlowConfig:
    window: int = 30               # 滚动历史窗口（期）
    min_history: int = 20          # 最少历史期数（预热）
    direction_z: float = 2.0       # F1: 失衡度 z-score 阈值
    volume_z: float = 2.0          # F2: 放量确认阈值
    cooldown_minutes: float = 15.0 # 同方向信号冷却
    max_strength_z: float = 4.0    # z-score → 强度的归一化上限


@dataclass
class _FlowState:
    imbalances: deque = field(default_factory=lambda: deque(maxlen=30))
    volumes: deque = field(default_factory=lambda: deque(maxlen=30))
    last_price: float | None = None
    price_5m_ago: float | None = None
    last_fired: dict[str, datetime] = field(default_factory=dict)


class FlowAnalyzer:
    def __init__(self, bus: EventBus, config: FlowConfig | None = None) -> None:
        self.bus = bus
        self.cfg = config or FlowConfig(window=30, min_history=20)
        self.state: dict[str, _FlowState] = {}

    def bind(self) -> None:
        self.bus.subscribe("flow", self.on_flow)
        self.bus.subscribe("kline", self.on_kline)   # 仅取价格做量价背离
        logger.info("资金流分析器已订阅 flow/kline（z=%.1f，窗口%d期）",
                    self.cfg.direction_z, self.cfg.window)

    async def on_kline(self, ev: KlineEvent) -> None:
        """记录价格序列（量价背离检测用），不参与其他逻辑。"""
        st = self.state.setdefault(ev.symbol, _FlowState())
        st.price_5m_ago, st.last_price = st.last_price, ev.closes()[-1]

    async def on_flow(self, ev: FlowEvent) -> None:
        st = self.state.setdefault(ev.symbol, _FlowState(
            imbalances=deque(maxlen=self.cfg.window),
            volumes=deque(maxlen=self.cfg.window)))

        imb = flow_math.volume_imbalance(ev.buy_volume, ev.sell_volume)
        # 预热期：只积累历史
        if len(st.imbalances) < self.cfg.min_history:
            st.imbalances.append(imb)
            st.volumes.append(ev.total_volume)
            return

        z_dir = flow_math.rolling_zscore(imb, list(st.imbalances))
        z_vol = flow_math.rolling_zscore(ev.total_volume, list(st.volumes))
        st.imbalances.append(imb)
        st.volumes.append(ev.total_volume)

        if abs(z_dir) < self.cfg.direction_z:
            return
        kind = "flow_in" if z_dir > 0 else "flow_out"
        direction = +1.0 if z_dir > 0 else -1.0
        now = datetime.now(timezone.utc)

        # 去抖：同方向冷却期内不重发
        last = st.last_fired.get(kind)
        if last and now - last < timedelta(minutes=self.cfg.cooldown_minutes):
            return
        st.last_fired[kind] = now

        # 强度：方向 z-score 归一化；放量确认则增强
        strength = min(abs(z_dir) / self.cfg.max_strength_z, 1.0)
        vol_confirmed = z_vol >= self.cfg.volume_z
        if vol_confirmed:
            strength = min(1.0, strength * 1.3 + 0.15)
        else:
            strength *= 0.7   # 未放量的方向异常降权（可能是小资金扰动）

        detail = (f"资金{'大额流入' if direction > 0 else '大额抛售'}: "
                  f"失衡度{imb:+.2f}(z={z_dir:+.1f}) 量能z={z_vol:+.1f}"
                  f"{' [放量确认]' if vol_confirmed else ''}")

        # F3 量价关系：同向确认增强，背离减半（大资金对倒出货陷阱）
        if st.last_price and st.price_5m_ago and st.price_5m_ago > 0:
            price_chg = (st.last_price - st.price_5m_ago) / st.price_5m_ago
            if direction * price_chg > 0 and abs(price_chg) > 0.001:
                strength = min(1.0, strength * 1.2)
                detail += f" [量价同向确认{price_chg:+.2%}]"
            elif direction * price_chg < 0 and abs(price_chg) > 0.001:
                strength *= 0.5
                detail += f" [量价背离{price_chg:+.2%}，强度减半]"

        sig = SignalEvent(
            symbol=ev.symbol, market=ev.market, kind=kind,
            direction=direction, strength=round(min(strength, 1.0), 4),
            detail=detail, indicator=round(imb, 4), threshold=self.cfg.direction_z,
            interval=ev.period)
        logger.info("[资金流] %s %s 强度%.2f | %s", kind, ev.symbol, sig.strength, detail)
        await self.bus.publish(sig)
