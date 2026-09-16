"""LLM 新闻分析器 —— 决策层的"非结构化信息理解"部件。

两种模式：
1. LLM 模式：配置了 OPENAI 兼容 API（可接 GLM/GPT/Claude）时，
   把新闻 + 知识层注入的术语上下文喂给模型，要求输出结构化 JSON。
   LLM 只出"观点"（情绪分/影响标的/置信度），绝不直接产出订单。
2. 关键词回退模式：无 API key 或调用失败时，用可解释的情绪词典打分，
   保证系统降级可用（系统级熔断原则：LLM 故障 ≠ 停摆，而是降级保守）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from common.events import NewsEvent
from knowledge_layer.knowledge_service import KnowledgeService

logger = logging.getLogger("llm_analyzer")

_SYSTEM_PROMPT = """你是一名宏观与加密市场的新闻分析师。分析新闻对市场的短期影响。
只输出 JSON，不要输出其他文字，格式：
{"sentiment": -1.0~1.0 的浮点数(正=利多风险资产,负=利空),
 "symbols": ["受影响标的如 BTC/USDT"],
 "horizon_minutes": 影响持续分钟数(整数),
 "confidence": 0~1,
 "reasoning": "一句话中文推理"}
不确定时 sentiment 取 0、confidence 取低值。"""

# 关键词回退模式的情绪词典（+利多 / -利空）
_KEYWORD_SENTIMENT: dict[str, float] = {
    "降息": +0.8, "宽松": +0.6, "放水": +0.6, "救市": +0.7, "降准": +0.6,
    "批准": +0.5, "利好": +0.7, "突破": +0.4, " adoption": +0.3, "采用": +0.4,
    "ETF获批": +0.8, "合作": +0.3, "升级": +0.3,
    "加息": -0.8, "鹰派": -0.6, "缩表": -0.6, "QT": -0.6, "紧缩": -0.6,
    "战争": -0.7, "导弹": -0.7, "空袭": -0.7, "制裁": -0.5, "黑客": -0.8,
    "被盗": -0.8, "崩盘": -0.9, "暴跌": -0.7, "禁令": -0.7, "诉讼": -0.4,
    "CPI超预期": -0.5, "通胀": -0.3,
}


@dataclass
class NewsAnalysis:
    sentiment: float          # -1 ~ 1
    symbols: list[str]
    horizon_minutes: int
    confidence: float         # 0 ~ 1
    reasoning: str
    engine: str               # "llm" | "keyword-fallback"


class LLMAnalyzer:
    def __init__(
        self,
        knowledge: KnowledgeService,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 20.0,
    ) -> None:
        self.knowledge = knowledge
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    @property
    def llm_available(self) -> bool:
        return bool(self.api_key)

    async def analyze(self, news: NewsEvent) -> NewsAnalysis:
        if self.llm_available:
            try:
                return await self._analyze_with_llm(news)
            except Exception:  # noqa: BLE001 —— LLM 故障降级到关键词模式
                logger.exception("LLM 调用失败，降级为关键词模式")
        return self._analyze_with_keywords(news)

    # ---------------------------------------------------------------- LLM

    async def _analyze_with_llm(self, news: NewsEvent) -> NewsAnalysis:
        from openai import AsyncOpenAI  # 可选依赖，懒加载

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        user_content = (
            f"{self.knowledge.llm_context_for(news.title + news.body)}\n\n"
            f"[新闻]\n标题: {news.title}\n正文: {news.body[:800]}\n"
            f"来源: {news.source} 级别: {news.priority.name}"
        )
        resp = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or "{}"
        data = json.loads(_strip_code_fence(text))
        return NewsAnalysis(
            sentiment=_clamp(float(data.get("sentiment", 0.0)), -1.0, 1.0),
            symbols=[str(s) for s in data.get("symbols", [])],
            horizon_minutes=int(data.get("horizon_minutes", 60)),
            confidence=_clamp(float(data.get("confidence", 0.3)), 0.0, 1.0),
            reasoning=str(data.get("reasoning", ""))[:300],
            engine="llm",
        )

    # ------------------------------------------------------------ 关键词回退

    def _analyze_with_keywords(self, news: NewsEvent) -> NewsAnalysis:
        text = f"{news.title} {news.body}"
        score = 0.0
        hits = 0
        for kw, s in _KEYWORD_SENTIMENT.items():
            if kw in text:
                score += s
                hits += 1
        sentiment = _clamp(score, -1.0, 1.0) if hits else 0.0
        # 置信度：命中越多、新闻级别越高越自信
        level_boost = {1: 0.3, 2: 0.2, 3: 0.1}[news.priority.value]
        confidence = _clamp(0.3 + 0.15 * (hits - 1) + level_boost, 0.0, 0.9) if hits else 0.1
        symbols = news.symbols or []
        return NewsAnalysis(
            sentiment=sentiment,
            symbols=symbols,
            horizon_minutes=60 if hits else 30,
            confidence=confidence,
            reasoning=f"关键词模式: 命中{hits}个情绪词, 合计{score:+.1f}" if hits else "无情绪词命中",
            engine="keyword-fallback",
        )


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()
