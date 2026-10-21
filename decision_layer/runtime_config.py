"""从 config/risk_limits.yaml 加载风控、策略、突破和资金流参数。

real_run 启动时以该文件为准。文件缺失或某一段缺字段时，保留代码里的默认值。
"""
from __future__ import annotations

import logging
from dataclasses import fields
from pathlib import Path

from decision_layer.breakout_detector import BreakoutConfig
from decision_layer.flow_analyzer import FlowConfig
from decision_layer.risk_manager import RiskLimits
from decision_layer.strategy import StrategyConfig

logger = logging.getLogger("runtime_config")

# 旧键名：配置文件曾把信号有效期写成 breakout_ttl_minutes
_STRATEGY_ALIASES = {"breakout_ttl_minutes": "signal_ttl_minutes"}


def load_runtime_config(
    path: Path,
) -> tuple[RiskLimits, StrategyConfig, BreakoutConfig, FlowConfig]:
    """读取运行参数。返回 (风控, 策略, 突破检测, 资金流)。"""
    limits, strategy = RiskLimits(), StrategyConfig()
    breakout, flow = BreakoutConfig(), FlowConfig()
    if not path.exists():
        logger.warning("未找到 %s，使用代码默认参数", path)
        return limits, strategy, breakout, flow
    try:
        import yaml
    except ImportError:
        logger.warning("未安装 pyyaml，使用代码默认参数")
        return limits, strategy, breakout, flow
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _apply(limits, data.get("risk_limits") or {})
    _apply(strategy, data.get("strategy") or {}, _STRATEGY_ALIASES)
    _apply(breakout, data.get("breakout") or {})
    _apply(flow, data.get("flow") or {})
    timeout = "关" if strategy.time_stop_minutes is None else f"{strategy.time_stop_minutes}分钟"
    logger.info(
        "已加载 %s | 止损=%.2f%% 止盈=%s 超时=%s 冷却=%d分钟 趋势过滤=%s 跟随=%s 挂单进场=%s",
        path.name,
        strategy.stop_loss_pct * 100,
        "关" if strategy.take_profit_pct is None else f"{strategy.take_profit_pct * 100:.2f}%",
        timeout,
        limits.cooldown_minutes,
        "开" if strategy.trend_filter else "关",
        "开" if strategy.follow_flow else "关",
        "开" if strategy.maker_entries else "关",
    )
    return limits, strategy, breakout, flow


def _apply(obj: object, raw: dict, aliases: dict[str, str] | None = None) -> None:
    """按数据类字段就地覆盖。未知键忽略；null 表示关闭可选规则。"""
    if not raw:
        return
    values = dict(raw)
    for alias, name in (aliases or {}).items():
        if alias in values and name not in values:
            values[name] = values[alias]
    known = {f.name for f in fields(obj)}
    for name, value in values.items():
        if name not in known:
            continue
        current = getattr(obj, name)
        setattr(obj, name, _coerce(current, value))


def _coerce(current: object, value: object) -> object:
    if value is None:
        return None
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value
