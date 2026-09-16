"""消息总线 —— 单机 asyncio 内存实现。

接口刻意与 Redis Streams 对齐（topic / consumer 语义），后续要多进程部署时，
只需把这个类换成 Redis 实现（redis.asyncio 的 xadd / xreadgroup），四层代码不动。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from common.events import Event

logger = logging.getLogger("bus")


class EventBus:
    """极简发布/订阅总线：publish 为 fire-and-forget，handler 异常不中断其他消费者。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Event], Awaitable[None]]]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable[[Event], Awaitable[None]]) -> None:
        self._handlers[topic].append(handler)
        logger.debug("subscribe %s -> %s", topic, getattr(handler, "__qualname__", handler))

    async def publish(self, event: Event) -> None:
        topic = event.topic()
        for handler in self._handlers.get(topic, []):
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 —— 单个消费者故障不能拖垮整条链路
                logger.exception("handler %s failed on %s", handler, topic)

    async def wait_idle(self, timeout: float = 1.0) -> None:
        """给在途事件一点处理时间（演示/测试用）。"""
        await asyncio.sleep(timeout)
