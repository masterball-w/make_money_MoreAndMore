"""市场规则手册 —— 两市场的交易制度差异（T+1、时段、最小单位、手续费）。

操作层下单前必须查询这里；决策层生成 A股 决策时也要遵守（例如当日买入不可卖）。
优先从 config/knowledge/market_rules.yaml 加载。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from common.events import Market

logger = logging.getLogger("market_rules")


@dataclass
class MarketRule:
    t_plus: int = 0                       # 0 = T+0（加密），1 = T+1（A股）
    trading_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # 0=周一
    sessions: list[tuple[time, time]] = field(default_factory=list)           # 交易时段
    fee_rate: float = 0.0                 # 手续费比例
    min_qty_step: float = 1.0             # 最小下单数量单位（A股一手=100股）
    price_limit_pct: float | None = None  # 涨跌停幅度；None = 无限制
    supports_short: bool = False          # 是否支持做空


_CRYPTO_RULE = MarketRule(
    t_plus=0,
    trading_days=[0, 1, 2, 3, 4, 5, 6],
    sessions=[],                          # 7x24
    fee_rate=0.001,
    min_qty_step=0.0001,
    price_limit_pct=None,
    supports_short=True,
)

_CN_STOCK_RULE = MarketRule(
    t_plus=1,
    trading_days=[0, 1, 2, 3, 4],
    sessions=[(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))],
    fee_rate=0.0005,                      # 佣金+印花税粗略合并估计
    min_qty_step=100,
    price_limit_pct=0.10,
    supports_short=False,
)


class MarketRuleBook:
    def __init__(self, config_path: Path | None = None) -> None:
        self._rules: dict[Market, MarketRule] = {
            Market.CRYPTO: _CRYPTO_RULE,
            Market.CN_STOCK: _CN_STOCK_RULE,
        }
        if config_path and config_path.exists():
            self._load_yaml(config_path)

    def _load_yaml(self, path: Path) -> None:
        try:
            import yaml
        except ImportError:
            logger.warning("pyyaml 未安装，使用内置市场规则: %s", path)
            return
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for name, rule in data.items():
            try:
                market = Market(name)
            except ValueError:
                continue
            merged = self._rules[market]
            merged.t_plus = int(rule.get("t_plus", merged.t_plus))
            merged.fee_rate = float(rule.get("fee_rate", merged.fee_rate))
            merged.min_qty_step = float(rule.get("min_qty_step", merged.min_qty_step))
            merged.supports_short = bool(rule.get("supports_short", merged.supports_short))
        logger.info("市场规则加载完成")

    def rule(self, market: Market) -> MarketRule:
        return self._rules[market]

    def round_qty(self, market: Market, qty: float) -> float:
        """按最小单位向下取整下单数量。"""
        step = self._rules[market].min_qty_step
        if step <= 0:
            return qty
        return int(qty / step) * step
