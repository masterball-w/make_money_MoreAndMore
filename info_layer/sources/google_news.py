"""Google News RSS 关键词订阅源 —— 覆盖美联储/中东战争/央行/AI大模型/加密。

无需 API key。URL 形如:
    https://news.google.com/rss/search?q=<关键词>&hl=zh-CN&gl=CN&ceid=CN:zh-Hans
预置订阅主题（PRESET_TOPICS）即用户要求的四类前沿信息；也可自定义 query。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import httpx

from common.events import NewsCategory
from common.net import retry_async
from info_layer.base import NewsSource, RawNews

logger = logging.getLogger("google_news")

# 注：不附加 gl/ceid 参数 —— 实测更简洁的 URL 过代理更稳定
_GNEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=zh-CN"

# 预置订阅主题：query -> (源名, 类目)
PRESET_TOPICS: dict[str, tuple[str, NewsCategory]] = {
    "美联储 利率 议息":     ("gnews-fed", NewsCategory.FED),
    "美联储 OR Federal Reserve": ("gnews-fed-en", NewsCategory.FED),
    "中东 战争 导弹":        ("gnews-geo", NewsCategory.GEO),
    "央行 货币政策 降准 降息": ("gnews-central-bank", NewsCategory.FED),
    "大模型 OpenAI 人工智能":  ("gnews-ai", NewsCategory.AI),
    "比特币 加密货币 监管":   ("gnews-crypto", NewsCategory.CRYPTO),
}


class GoogleNewsSource(NewsSource):
    name = "gnews"
    category = NewsCategory.CRYPTO

    def __init__(self, query: str, name: str | None = None,
                 category: NewsCategory = NewsCategory.CRYPTO,
                 max_items: int = 15, timeout: float = 10.0) -> None:
        self.query = query
        self.name = name or f"gnews[{query[:12]}]"
        self.category = category
        self.max_items = max_items
        self.timeout = timeout
        self._seen: set[str] = set()

    async def fetch(self) -> list[RawNews]:
        url = _GNEWS_RSS.format(q=quote(self.query))

        async def _get() -> str:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                         headers={"User-Agent": "trading-system/0.1"}) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text

        # 代理链路对 Google 间歇性失败：带退避重试
        xml = await retry_async(_get, retries=3, backoff=1.5, label=f"gnews[{self.query[:10]}]")
        if xml is None:
            return []
        parsed = await asyncio.to_thread(feedparser.parse, xml)
        out: list[RawNews] = []
        for entry in parsed.entries[: self.max_items]:
            title = getattr(entry, "title", "")
            link = getattr(entry, "link", "")
            if not title or link in self._seen:
                continue
            published = getattr(entry, "published_parsed", None)
            self._seen.add(link)
            out.append(RawNews(
                title=title,
                body=getattr(entry, "summary", "")[:500],
                url=link,
                published_at=datetime(*published[:6], tzinfo=timezone.utc) if published
                else datetime.now(timezone.utc),
                source=self.name,
                category=self.category,
            ))
        return out

    @staticmethod
    def presets() -> list["GoogleNewsSource"]:
        """按用户要求的四类信息生成预置源集合。"""
        return [
            GoogleNewsSource(q, name=n, category=c)
            for q, (n, c) in PRESET_TOPICS.items()
        ]
