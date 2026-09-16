"""信息源抽象 —— 新增一个新闻源只需实现 NewsSource 一个类。"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.events import NewsCategory


@dataclass
class RawNews:
    """信息源产出的原始新闻（未去重、未分级）。"""

    title: str
    body: str = ""
    url: str = ""
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = ""
    category: NewsCategory = NewsCategory.CRYPTO


class NewsSource(abc.ABC):
    """新闻源适配器接口。

    实现类负责某一类信息源的抓取细节（RSS / API / 爬虫），
    不负责去重与分级 —— 那是 pipeline 的事。
    """

    name: str = "base"
    category: NewsCategory = NewsCategory.CRYPTO

    @abc.abstractmethod
    async def fetch(self) -> list[RawNews]:
        """拉取一批最新新闻。实现应快速失败，异常由聚合器捕获并告警。"""

    async def health_check(self) -> bool:
        """信息源健康检查（连续失败会触发系统级告警/降级）。"""
        return True
