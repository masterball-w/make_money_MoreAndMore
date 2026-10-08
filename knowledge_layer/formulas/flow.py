"""资金流公式 —— 量能失衡与异常检测（底层逻辑计算层，纯函数）。

概念定义（供代码与 LLM 共用）：
- 主动买量(taker buy): 买方主动吃掉卖盘的成交量，代表买方急迫度
- 主动卖量(taker sell): 卖方主动砸向买盘的成交量，代表抛售压力
- 净流入(net flow): 买-卖；失衡度(imbalance): (买-卖)/(买+卖) ∈ [-1,1]
- 异常检测：滚动 z-score —— 当前失衡/量能相对自身历史分布偏离几个标准差
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def volume_imbalance(buy_volume: float, sell_volume: float) -> float:
    """量能失衡度 (买-卖)/(买+卖)，∈ [-1, 1]。0 = 均衡，+1 = 纯买，-1 = 纯卖。"""
    total = buy_volume + sell_volume
    if total <= 0:
        return 0.0
    return (buy_volume - sell_volume) / total


def rolling_mean_std(values: Sequence[float]) -> tuple[float, float]:
    """序列的均值与样本标准差。"""
    n = len(values)
    if n < 2:
        return (values[0] if values else 0.0), 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


def zscore(value: float, mean: float, std: float) -> float:
    """标准分：(value - mean) / std。

    std=0 的极限处理：值与均值一致 → 0（无异常）；不一致 → ±10
    （零波动基线上任何偏离都是极端显著的，如恒定流量中突现脉冲）。
    """
    if std <= 0:
        if value == mean:
            return 0.0
        return math.copysign(10.0, value - mean)
    return (value - mean) / std


def rolling_zscore(value: float, history: Sequence[float]) -> float:
    """当前值相对历史窗口的 z-score（异常检测核心公式）。"""
    mean, std = rolling_mean_std(history)
    return zscore(value, mean, std)


def flow_intensity(buy_volume: float, sell_volume: float, period_seconds: int = 300) -> float:
    """资金流强度（单位时间净流入速率），用于跨周期比较。"""
    if period_seconds <= 0:
        return 0.0
    return (buy_volume - sell_volume) / period_seconds
