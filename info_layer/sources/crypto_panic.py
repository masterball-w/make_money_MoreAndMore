"""CryptoPanic 新闻源 —— 加密行业快讯（阶段2实装，当前为接口占位）。

文档: https://cryptopanic.com/developers/api/
需要免费 API token。依赖 httpx（可选）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from common.events import NewsCategory
from info_layer.base import NewsSource, RawNews

logger = logging.getLogger("crypto_panic")


class CryptoPanicSource(NewsSource):
    name = "cryptopanic"
    category = NewsCategory.CRYPTO
    _API = "https://cryptopanic.com/api/v1/posts/?auth_token={token}&public=true&currencies=BTC,ETH"

    def __init__(self, token: str) -> None:
        self.token = token

    async def fetch(self) -> list[RawNews]:
        try:
            import httpx
        except ImportError:
            logger.error("httpx 未安装：pip install httpx")
            return []
        if not self.token:
            logger.warning("CryptoPanic token 未配置，跳过")
            return []
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self._API.format(token=self.token))
            resp.raise_for_status()
            data = resp.json()
        out: list[RawNews] = []
        for post in data.get("results", [])[:20]:
            out.append(
                RawNews(
                    title=post.get("title", ""),
                    body="",
                    url=post.get("url", ""),
                    published_at=datetime.now(timezone.utc),
                    source=self.name,
                    category=self.category,
                )
            )
        return out
