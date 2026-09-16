"""模拟新闻源 —— 演示与测试用：按脚本预置新闻，可控制推送节奏。"""
from __future__ import annotations

import asyncio

from common.events import NewsCategory
from info_layer.base import NewsSource, RawNews


class MockNewsSource(NewsSource):
    name = "mock"
    category = NewsCategory.FED

    def __init__(self, script: list[RawNews] | None = None, interval: float = 1.0) -> None:
        self.script = script or []
        self.interval = interval
        self._cursor = 0
        self.published: list[RawNews] = []

    async def fetch(self) -> list[RawNews]:
        """每次拉取返回下一条脚本新闻；播完后返回空（模拟无新消息）。"""
        if self._cursor >= len(self.script):
            return []
        item = self.script[self._cursor]
        self._cursor += 1
        self.published.append(item)
        await asyncio.sleep(0)  # 模拟网络 IO 的让出点
        return [item]
