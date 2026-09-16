"""技术指标库 —— SMA / EMA / MACD / RSI / 布林带 / ATR。纯函数实现。"""
from __future__ import annotations

from collections.abc import Sequence


def sma(prices: Sequence[float], period: int) -> list[float | None]:
    """简单移动平均。前 period-1 个位置为 None（数据不足）。"""
    out: list[float | None] = [None] * len(prices)
    if period <= 0:
        return out
    rolling = 0.0
    for i, p in enumerate(prices):
        rolling += p
        if i >= period:
            rolling -= prices[i - period]
        if i >= period - 1:
            out[i] = rolling / period
    return out


def ema(prices: Sequence[float], period: int) -> list[float | None]:
    """指数移动平均，首值用前 period 个价格的 SMA 初始化。"""
    out: list[float | None] = [None] * len(prices)
    if len(prices) < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(prices[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(prices)):
        prev = prices[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


def macd(
    prices: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD：返回 (dif, dea, histogram)。dif=EMA(fast)-EMA(slow)，dea=dif 的 EMA。"""
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    dif: list[float | None] = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    # 对 dif 的有效段计算 signal EMA
    valid_start = next((i for i, v in enumerate(dif) if v is not None), None)
    dea: list[float | None] = [None] * len(prices)
    hist: list[float | None] = [None] * len(prices)
    if valid_start is not None:
        valid = [v for v in dif[valid_start:] if v is not None]  # type: ignore[misc]
        dea_valid = ema(valid, signal)
        offset = valid_start
        for i, v in enumerate(dea_valid):
            if v is not None:
                dea[offset + i] = v
        for i in range(len(prices)):
            if dif[i] is not None and dea[i] is not None:
                hist[i] = dif[i] - dea[i]  # type: ignore[operator]
    return dif, dea, hist


def rsi(prices: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder RSI。"""
    out: list[float | None] = [None] * len(prices)
    if len(prices) <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        change = prices[i] - prices[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(prices)):
        change = prices[i] - prices[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(
    prices: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """布林带：返回 (中轨=SMA, 上轨, 下轨)。"""
    mid = sma(prices, period)
    upper: list[float | None] = [None] * len(prices)
    lower: list[float | None] = [None] * len(prices)
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        m = mid[i]
        assert m is not None
        var = sum((p - m) ** 2 for p in window) / period
        std = var**0.5
        upper[i] = m + num_std * std
        lower[i] = m - num_std * std
    return mid, upper, lower


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float | None]:
    """平均真实波幅 (Wilder 平滑)。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    prev = sum(trs[:period]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i - 1]) / period
        out[i] = prev
    return out
