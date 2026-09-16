"""通用 RSS 新闻源 —— 美联储/央行/Reuters/官方博客等。

依赖 feedparser（可选）。配置示例见 config/settings.yaml 的 sources 段。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from common.events import NewsCategory
from info_layer.base import NewsSource, RawNews

logger = logging.getLogger("rss_source")


def _parse_dt(entry) -> datetime:  # noqa: ANN001 - feedparser 的 Entry 对象
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class RSSSource(NewsSource):
    def __init__(self, name: str, url: str, category: NewsCategory) -> None:
        self.name = name
        self.url = url
        self.category = category

    async def fetch(self) -> list[RawNews]:
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser 未安装：pip install feedparser （源 %s）", self.name)
            return []
        d = await __import__("asyncio").get_running_loop().run_in_executor(
            None, lambda: feedparser.parse(self.url)
        )
        out: list[RawNews] = []
        for entry in d.entries[:20]:
            out.append(
                RawNews(
                    title=getattr(entry, "title", ""),
                    body=getattr(entry, "summary", "")[:1000],
                    url=getattr(entry, "link", ""),
                    published_at=_parse_dt(entry),
                    source=self.name,
                    category=self.category,
                )
            )
        return out
