"""信息聚合器 —— 实时信息层的编排入口。

职责：周期性轮询所有新闻源、K 线源与行情源 → 管道去重分级 → 发布事件到总线。
某个信息源失败只记日志并计入连续失败计数（系统级降级由决策层风控感知）。
"""
from __future__ import annotations

import asyncio
import logging

from common.bus import EventBus
from common.events import Market, MarketEvent, Priority
from info_layer.base import NewsSource
from info_layer.flow import FlowProvider, _NoNewData
from info_layer.kline import KlineProvider
from info_layer.market_data import MarketDataProvider
from info_layer.pipeline import NewsPipeline

logger = logging.getLogger("info_aggregator")


class InfoAggregator:
    def __init__(
        self,
        bus: EventBus,
        sources: list[NewsSource],
        market_data: dict[str, MarketDataProvider],
        kline_data: dict[str, KlineProvider] | None = None,
        flow_data: dict[str, FlowProvider] | None = None,
        poll_interval: float = 5.0,
        market_poll_interval: float = 2.0,
        kline_poll_interval: float = 1.0,
        flow_poll_interval: float = 60.0,
        source_failure_alert_threshold: int = 5,
    ) -> None:
        self.bus = bus
        self.sources = sources
        self.market_data = market_data
        self.kline_data = kline_data or {}
        self.flow_data = flow_data or {}
        self.pipeline = NewsPipeline()
        self.poll_interval = poll_interval
        self.market_poll_interval = market_poll_interval
        self.kline_poll_interval = kline_poll_interval
        self.flow_poll_interval = flow_poll_interval
        self.source_failures: dict[str, int] = {s.name: 0 for s in sources}
        self._failure_alert_threshold = source_failure_alert_threshold
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ 新闻循环

    async def _poll_news_once(self) -> None:
        for source in self.sources:
            try:
                items = await source.fetch()
            except Exception:  # noqa: BLE001
                self.source_failures[source.name] += 1
                count = self.source_failures[source.name]
                logger.exception("新闻源 %s 抓取失败（连续 %d 次）", source.name, count)
                if count == self._failure_alert_threshold:
                    logger.error("[系统级告警] 信息源 %s 连续失败 %d 次，请检查网络/凭证",
                                 source.name, count)
                continue
            self.source_failures[source.name] = 0
            for raw in items:
                event = self.pipeline.process(raw)
                if event is None:
                    logger.info("重复新闻已过滤: %s", raw.title[:40])
                    continue
                tag = event.priority.name
                logger.info("[信息层] (%s/%s) %s", tag, event.category.value, event.title[:60])
                # P1 走快速通道：立即发布；其余同样立即发布但下游可按级别差异化处理
                await self.bus.publish(event)

    # ------------------------------------------------------------ 行情循环

    async def _poll_market_once(self) -> None:
        for symbol, provider in self.market_data.items():
            try:
                snap: MarketEvent = await provider.snapshot(symbol)
            except NotImplementedError:
                logger.warning("行情源未实装，跳过 %s", symbol)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("行情抓取失败: %s", symbol)
                continue
            await self.bus.publish(snap)

    async def _poll_kline_once(self) -> None:
        """K 线流轮询：发布 KlineEvent（突破检测器订阅）。"""
        for symbol, provider in self.kline_data.items():
            try:
                kline = await provider.next(symbol)
            except NotImplementedError:
                logger.warning("K线源未实装，跳过 %s", symbol)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("K线抓取失败: %s", symbol)
                continue
            await self.bus.publish(kline)

    async def _poll_flow_once(self) -> None:
        """资金流轮询：发布 FlowEvent（资金流分析器订阅）。"""
        for symbol, provider in self.flow_data.items():
            try:
                flow_ev = await provider.next(symbol)
            except _NoNewData:
                continue          # 统计周期未滚动，正常现象
            except NotImplementedError:
                logger.warning("资金流源未实装，跳过 %s", symbol)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("资金流抓取失败: %s", symbol)
                continue
            await self.bus.publish(flow_ev)

    # ------------------------------------------------------------ 生命周期

    async def _news_loop(self) -> None:
        while not self._stop.is_set():
            await self._poll_news_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _market_loop(self) -> None:
        while not self._stop.is_set():
            await self._poll_market_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.market_poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _kline_loop(self) -> None:
        while not self._stop.is_set():
            await self._poll_kline_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.kline_poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _flow_loop(self) -> None:
        while not self._stop.is_set():
            await self._poll_flow_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.flow_poll_interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._news_loop(), name="news-loop"),
            asyncio.create_task(self._market_loop(), name="market-loop"),
            asyncio.create_task(self._kline_loop(), name="kline-loop"),
            asyncio.create_task(self._flow_loop(), name="flow-loop"),
        ]
        logger.info("信息聚合器启动: %d 个新闻源, %d 个行情源, %d 个K线源, %d 个资金流源",
                    len(self.sources), len(self.market_data),
                    len(self.kline_data), len(self.flow_data))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("信息聚合器停止")

    # 供回测/演示注入事件 -------------------------------------------------

    async def ingest_raw(self, raw) -> None:  # noqa: ANN001 - RawNews
        event = self.pipeline.process(raw)
        if event is not None:
            await self.bus.publish(event)
        else:
            logger.info("[信息层] 重复新闻已过滤: %s", raw.title[:40])

    async def push_kline(self, kline) -> None:  # noqa: ANN001 - KlineEvent
        """直接发布 K 线事件（演示/回测注入用；实盘走 _kline_loop）。"""
        await self.bus.publish(kline)
