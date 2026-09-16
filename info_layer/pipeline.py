"""信息管道 —— 去重（内容指纹）+ 重要性分级（P1/P2/P3）+ 标的关联。

规则可配置、可解释：每一步都是简单关键词规则，方便审计与调参。
"""
from __future__ import annotations

import hashlib
import re

from common.events import NewsCategory, NewsEvent, Priority
from info_layer.base import RawNews

# ---------------------------------------------------------------- 分级规则

# P1 紧急：直接改变宏观定价或引发避险的事件
_P1_KEYWORDS = [
    "降息", "加息", "议息", "FOMC", "联邦基金利率", "紧急降息",
    "宣战", "开战", "导弹袭击", "空袭", "停火", "政权", "政变",
    "降准", "LPR", "印花税", "救市",
]
# P2 重要：影响行业与资金面
_P2_KEYWORDS = [
    "非农", "CPI", "PCE", "缩表", "QT", "量化紧缩", "点阵图", "鲍威尔", "主席",
    "ETF", "批准", "监管", "制裁", "原油", "黄金", "避险",
    "大模型", "GPT", "OpenAI", "融资", "算力", "芯片",
    "央行", "MLF", "逆回购", "北向资金",
]

# ---------------------------------------------------------------- 标的关联

_SYMBOL_MAP: dict[str, list[str]] = {
    "美联储": ["BTC/USDT", "ETH/USDT"],
    "FOMC": ["BTC/USDT"],
    "降息": ["BTC/USDT", "ETH/USDT"],
    "加息": ["BTC/USDT", "ETH/USDT"],
    "中东": ["BTC/USDT"],
    "战争": ["BTC/USDT"],
    "导弹": ["BTC/USDT"],
    "比特币": ["BTC/USDT"],
    "以太坊": ["ETH/USDT"],
    "ETF": ["BTC/USDT"],
    "大模型": ["BTC/USDT"],
    "OpenAI": ["BTC/USDT"],
    "央行": ["BTC/USDT"],
    "加密": ["BTC/USDT", "ETH/USDT"],
    "区块链": ["BTC/USDT", "ETH/USDT"],
}


def normalize_title(title: str) -> str:
    """标题归一化：去空白与标点，统一小写 —— 让指纹对转载改写更鲁棒。"""
    return re.sub(r"[\s\W_]+", "", title, flags=re.UNICODE).lower()


def fingerprint(raw: RawNews) -> str:
    return hashlib.sha256(normalize_title(raw.title).encode("utf-8")).hexdigest()[:16]


def classify_priority(text: str) -> Priority:
    if any(k in text for k in _P1_KEYWORDS):
        return Priority.P1
    if any(k in text for k in _P2_KEYWORDS):
        return Priority.P2
    return Priority.P3


def extract_symbols(text: str) -> list[str]:
    seen: list[str] = []
    for kw, symbols in _SYMBOL_MAP.items():
        if kw in text:
            for s in symbols:
                if s not in seen:
                    seen.append(s)
    return seen


class NewsPipeline:
    """去重 + 分级 + 标的关联。seen 集合有上限，防止长期运行内存膨胀。"""

    def __init__(self, max_seen: int = 50_000) -> None:
        self._seen: dict[str, int] = {}  # fingerprint -> 序号（用于淘汰）
        self._counter = 0
        self._max_seen = max_seen

    def process(self, raw: RawNews) -> NewsEvent | None:
        """返回 None 表示重复新闻，被过滤。"""
        fp = fingerprint(raw)
        if fp in self._seen:
            return None
        self._counter += 1
        self._seen[fp] = self._counter
        if len(self._seen) > self._max_seen:
            # 淘汰最旧的一半
            keep = sorted(self._seen.items(), key=lambda kv: kv[1])[-self._max_seen // 2 :]
            self._seen = dict(keep)

        text = f"{raw.title} {raw.body}"
        return NewsEvent(
            title=raw.title,
            body=raw.body,
            url=raw.url,
            source=raw.source,
            published_at=raw.published_at,
            category=raw.category,
            priority=classify_priority(text),
            symbols=extract_symbols(text),
            fingerprint=fp,
        )
