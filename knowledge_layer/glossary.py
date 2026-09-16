"""金融术语词典 —— 底层逻辑计算层的知识配置。

用途有二：
1. 代码内查询（决策层解读新闻关键词时）；
2. 作为 LLM 的知识上下文注入（让 LLM 准确理解"鹰派/缩表/QT"等术语在交易语境下的含义）。
优先从 config/knowledge/glossary.yaml 加载；文件缺失时退回内置最小集。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("glossary")

# 内置最小词典（yaml 不可用/配置缺失时的兜底）
_BUILTIN_TERMS: dict[str, str] = {
    "鹰派": "主张收紧货币政策（加息/缩表），通常利空风险资产。",
    "鸽派": "主张宽松货币政策（降息/放水），通常利多风险资产。",
    "加息": "央行提高政策利率，收紧流动性，通常利空股票与加密货币。",
    "降息": "央行降低政策利率，释放流动性，通常利多风险资产。",
    "缩表": "央行缩减资产负债表，从市场回收流动性，效果类似加息。",
    "QT": "量化紧缩 (Quantitative Tightening)，即缩表，利空流动性敏感资产。",
    "FOMC": "美联储联邦公开市场委员会，其议息决议直接决定美元利率政策。",
    "非农": "美国非农就业报告，衡量美国就业强弱，影响美联储政策预期。",
    "CPI": "消费者价格指数，通胀指标；高通胀预期会强化紧缩政策预期。",
    "T+1": "A股当日买入的股票次日才能卖出；加密货币为 T+0。",
    "止损": "价格达到预设亏损水平时强制平仓，限制单笔损失。",
    "VaR": "在险价值：给定置信水平下一定持有期的最大预期损失。",
    "凯利公式": "按胜率与盈亏比计算最优仓位比例的公式。",
    "流动性": "资产能够快速买卖而不显著影响价格的程度。",
}


class Glossary:
    def __init__(self, config_path: Path | None = None) -> None:
        self._terms: dict[str, str] = dict(_BUILTIN_TERMS)
        if config_path and config_path.exists():
            self._load_yaml(config_path)

    def _load_yaml(self, path: Path) -> None:
        try:
            import yaml  # 可选依赖
        except ImportError:
            logger.warning("pyyaml 未安装，使用内置术语集: %s", path)
            return
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for item in data.get("terms", []):
            term = str(item.get("term", "")).strip()
            definition = str(item.get("definition", "")).strip()
            if term and definition:
                self._terms[term] = definition
        logger.info("术语词典加载完成: %d 条", len(self._terms))

    def define(self, term: str) -> str | None:
        return self._terms.get(term)

    @property
    def all_terms(self) -> dict[str, str]:
        return dict(self._terms)

    def context_for_text(self, text: str) -> dict[str, str]:
        """返回文本中出现的术语及其解释 —— 用于给 LLM 注入知识上下文。"""
        return {t: d for t, d in self._terms.items() if t in text}
