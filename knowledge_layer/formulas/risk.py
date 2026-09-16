"""风险与绩效公式 —— VaR、年化波动率、最大回撤、夏普比率、凯利仓位、风险仓位法。

所有函数接收普通序列（list[float]），返回标量；无任何副作用。
"""
from __future__ import annotations

import math
from collections.abc import Sequence

# 常用年化因子：加密 7x24 小时线、A股日线约 252 个交易日、周线 52
PERIODS_CRYPTO_HOURLY = 24 * 365
PERIODS_DAILY = 252


def simple_returns(prices: Sequence[float]) -> list[float]:
    """简单收益率序列 r_t = p_t / p_{t-1} - 1。"""
    if len(prices) < 2:
        return []
    return [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)) if prices[i - 1] != 0]


def annualized_volatility(returns: Sequence[float], periods_per_year: int = PERIODS_DAILY) -> float:
    """年化波动率 = 收益率标准差 * sqrt(周期数/年)。"""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> tuple[float, int, int]:
    """最大回撤：返回 (回撤比例, 峰值索引, 谷值索引)。"""
    peak = equity_curve[0] if equity_curve else 0.0
    peak_idx = 0
    mdd, mdd_peak, mdd_trough = 0.0, 0, 0
    for i, v in enumerate(equity_curve):
        if v > peak:
            peak, peak_idx = v, i
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd, mdd_peak, mdd_trough = dd, peak_idx, i
    return mdd, mdd_peak, mdd_trough


def var_historical(returns: Sequence[float], confidence: float = 0.95) -> float:
    """历史模拟法 VaR：给定置信水平下，单期最大预期损失（返回正数表示损失）。

    若分位数处为正收益（最差情形也不亏），则 VaR = 0。
    """
    if not returns:
        return 0.0
    sorted_r = sorted(returns)
    idx = max(0, int((1 - confidence) * len(sorted_r)) - 1)
    return abs(min(sorted_r[idx], 0.0))


def sharpe_ratio(
    returns: Sequence[float],
    risk_free_per_period: float = 0.0,
    periods_per_year: int = PERIODS_DAILY,
) -> float:
    """年化夏普比率 = (单期均值收益 - 无风险) / 单期标准差 * sqrt(年化周期)。"""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean - risk_free_per_period) / std * math.sqrt(periods_per_year)


def kelly_fraction(win_prob: float, win_loss_ratio: float) -> float:
    """凯利公式 f* = p - (1-p)/b。返回建议投入资金比例，负值意味着不应下注。"""
    if not 0.0 < win_prob < 1.0:
        return 0.0
    if win_loss_ratio <= 0:
        return 0.0
    return win_prob - (1.0 - win_prob) / win_loss_ratio


def position_size_by_risk(
    capital: float,
    risk_per_trade: float,
    entry_price: float,
    stop_price: float,
) -> float:
    """风险仓位法：数量 = (资金 * 单笔风险比例) / |入场价 - 止损价|。

    这是本系统"单笔风险 ≤ 账户 1%"的落地公式。
    """
    if entry_price <= 0 or stop_price <= 0:
        return 0.0
    per_unit_risk = abs(entry_price - stop_price)
    if per_unit_risk == 0:
        return 0.0
    return capital * risk_per_trade / per_unit_risk


def stop_loss_price(entry_price: float, stop_pct: float, is_long: bool = True) -> float:
    """按比例止损价。多头：entry*(1-pct)；空头：entry*(1+pct)。"""
    if is_long:
        return entry_price * (1.0 - stop_pct)
    return entry_price * (1.0 + stop_pct)
