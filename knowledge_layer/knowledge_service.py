"""知识服务 —— 底层逻辑计算层对外（决策层/LLM）的统一门面。"""
from __future__ import annotations

from pathlib import Path

from common.events import Market
from knowledge_layer.formulas import risk
from knowledge_layer.glossary import Glossary
from knowledge_layer import indicators
from knowledge_layer.market_rules import MarketRule, MarketRuleBook

# 默认配置目录：项目根/config/knowledge
_DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "config" / "knowledge"


class KnowledgeService:
    """聚合公式库、指标库、术语词典与市场规则的只读服务。"""

    def __init__(self, knowledge_dir: Path | None = None) -> None:
        base = knowledge_dir or _DEFAULT_KNOWLEDGE_DIR
        self.glossary = Glossary(base / "glossary.yaml")
        self.rulebook = MarketRuleBook(base / "market_rules.yaml")
        # 公式与指标是纯函数模块，直接以属性暴露
        self.risk = risk
        self.indicators = indicators

    def market_rule(self, market: Market) -> MarketRule:
        return self.rulebook.rule(market)

    def llm_context_for(self, text: str) -> str:
        """拼装一段给 LLM 的知识上下文：文中出现的术语解释 + 关键风险原则。"""
        terms = self.glossary.context_for_text(text)
        lines = ["[术语参考]"]
        lines += [f"- {t}: {d}" for t, d in terms.items()]
        lines.append("[交易原则] 单笔风险不超过账户1%；重大事件窗口内降仓；不确定时保持观望(HOLD)。")
        return "\n".join(lines)
