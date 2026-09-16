"""量化引擎 —— 基于底层逻辑计算层的指标，输出技术面信号 (-1 ~ +1)。

信号规则（可解释、可回测）：
- 趋势: EMA(12) 在 EMA(26) 之上且价格 > EMA(26) → 看多加成
- 动量: RSI < 30 超卖看多 / RSI > 70 超买看空（回归逻辑）
- 波带: 价格跌破布林下轨偏多 / 突破上轨偏空（均值回归），仅强趋势时反向豁免
"""
from __future__ import annotations

from dataclasses import dataclass

from common.events import MarketEvent
from knowledge_layer import indicators


@dataclass
class QuantSignal:
    value: float            # -1 ~ +1
    detail: str


class QuantEngine:
    def signal(self, market: MarketEvent) -> QuantSignal | None:
        closes = market.history
        if len(closes) < 30:
            return None

        price = market.price
        ema12 = indicators.ema(closes, 12)
        ema26 = indicators.ema(closes, 26)
        rsi14 = indicators.rsi(closes, 14)
        mid, upper, lower = indicators.bollinger(closes, 20, 2.0)

        score = 0.0
        parts: list[str] = []

        # 趋势
        if ema12[-1] is not None and ema26[-1] is not None:
            trend_up = ema12[-1] > ema26[-1] and price > ema26[-1]
            trend_dn = ema12[-1] < ema26[-1] and price < ema26[-1]
            if trend_up:
                score += 0.4
                parts.append("EMA趋势↑")
            elif trend_dn:
                score -= 0.4
                parts.append("EMA趋势↓")

        # 动量（RSI 超买超卖）
        if rsi14[-1] is not None:
            r = rsi14[-1]
            if r < 30:
                score += 0.35
                parts.append(f"RSI超卖{r:.0f}")
            elif r > 70:
                score -= 0.35
                parts.append(f"RSI超买{r:.0f}")

        # 布林带均值回归（趋势不强时生效）
        if upper[-1] is not None and lower[-1] is not None:
            if price <= lower[-1] and abs(score) < 0.4:
                score += 0.3
                parts.append("触及布林下轨")
            elif price >= upper[-1] and abs(score) < 0.4:
                score -= 0.3
                parts.append("触及布林上轨")

        value = max(-1.0, min(1.0, score))
        return QuantSignal(value=value, detail="+".join(parts) if parts else "中性")
