"""通用 RSS 新闻源 —— 美联储/央行官网、Cointelegraph 等直接订阅。

依赖 feedparser + httpx（真实模式）。配置示例见 config/settings.yaml 的 sources 段。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from common.events import NewsCategory
from common.net import retry_async
from info_layer.base import NewsSource, RawNews

logger = logging.getLogger("rss_source")

# 本机网络实测可达的官方/权威源
KNOWN_FEEDS: dict[str, tuple[str, NewsCategory]] = {
    "federal-reserve": ("https://www.federalreserve.gov/feeds/press_monetary.xml", NewsCategory.FED),
    "cointelegraph": ("https://cointelegraph.com/rss", NewsCategory.CRYPTO),
}


def _parse_dt(entry) -> datetime:  # noqa: ANN001 - feedparser Entry
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class RSSSource(NewsSource):
    def __init__(self, name: str, url: str, category: NewsCategory,
                 max_items: int = 15, timeout: float = 10.0) -> None:
        self.name = name
        self.url = url
        self.category = category
        self.max_items = max_items
        self.timeout = timeout
        self._seen: set[str] = set()

    async def fetch(self) -> list[RawNews]:
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser 未安装：pip install feedparser （源 %s）", self.name)
            return []
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser 未安装：pip install feedparser （源 %s）", self.name)
            return []

        async def _get() -> str:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                         headers={"User-Agent": "trading-system/0.1"}) as client:
                resp = await client.get(self.url)
                resp.raise_for_status()
                return resp.text

        xml = await retry_async(_get, retries=3, backoff=1.5, label=self.name)
        if xml is None:
            return []
        d = await asyncio.to_thread(feedparser.parse, xml)
        out: list[RawNews] = []
        for entry in d.entries[: self.max_items]:
            link = getattr(entry, "link", "")
            if link and link in self._seen:
                continue
            if link:
                self._seen.add(link)
            out.append(RawNews(
                title=getattr(entry, "title", ""),
                body=getattr(entry, "summary", "")[:500],
                url=link,
                published_at=_parse_dt(entry),
                source=self.name,
                category=self.category,
            ))
        return out
