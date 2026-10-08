"""网络层通用工具 —— 带指数退避的重试（真实数据源的间歇性故障兜底）。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger("net")

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    retries: int = 3,
    backoff: float = 1.0,
    label: str = "",
) -> T | None:
    """执行 fn()，失败重试（指数退避 backoff * 2^n）。

    全部失败返回 None（调用方按"本轮无数据"降级处理，不抛异常拖垮轮询循环）。
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                logger.debug("重试 %s 第%d次失败(%s)，%.1fs 后重试",
                             label, attempt + 1, type(e).__name__, wait)
                await asyncio.sleep(wait)
    logger.warning("重试 %s 全部失败: %s", label, last_exc)
    return None
