"""真实试运行入口 —— 真实数据、模拟撮合（paper 模式）。

与 main.py（全 Mock 确定性演示）不同，本入口接入真实世界数据：
  行情: OKX/Binance 公开 REST K 线（无需 API key，主备自动切换）
  新闻: 美联储官网 RSS + Google News 关键词订阅（美联储/中东/央行/AI/加密）
  决策: 关键词情绪模式（设置 OPENAI_API_KEY 启用 LLM）
  撮合: PaperBroker（真实价格、含滑点手续费，不动真钱）

用法:
    python real_run.py                     # 默认跑 300 秒
    python real_run.py --duration 1800     # 跑 30 分钟
    python real_run.py --symbol ETH/USDT --interval 5m
Ctrl+C 可随时安全退出（优雅停止 + 输出汇总）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.bus import EventBus                              # noqa: E402
from common.events import KlineEvent                         # noqa: E402
from common.models import Account                            # noqa: E402
from decision_layer.audit import AuditLog                    # noqa: E402
from decision_layer.breakout_detector import BreakoutConfig, BreakoutDetector  # noqa: E402
from decision_layer.event_calendar import EconomicCalendar   # noqa: E402
from decision_layer.flow_analyzer import FlowAnalyzer, FlowConfig  # noqa: E402
from decision_layer.llm_analyzer import LLMAnalyzer          # noqa: E402
from decision_layer.quant_engine import QuantEngine          # noqa: E402
from decision_layer.risk_manager import RiskManager          # noqa: E402
from decision_layer.strategy import StrategyConfig, StrategyOrchestrator  # noqa: E402
from execution_layer.brokers.paper_broker import PaperBroker  # noqa: E402
from execution_layer.executor import Executor                # noqa: E402
from info_layer.aggregator import InfoAggregator             # noqa: E402
from info_layer.flow import CryptoFlowSource                 # noqa: E402
from info_layer.rest_kline import RESTKlineSource            # noqa: E402
from info_layer.sources.google_news import GoogleNewsSource  # noqa: E402
from info_layer.sources.rss_source import KNOWN_FEEDS, RSSSource  # noqa: E402
from knowledge_layer.knowledge_service import KnowledgeService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("real_run")

INITIAL_CASH = 100_000.0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="真实数据模拟盘试运行")
    p.add_argument("--duration", type=int, default=300, help="运行秒数（默认300）")
    p.add_argument("--symbol", default="BTC/USDT", help="交易标的（默认 BTC/USDT）")
    p.add_argument("--interval", default="1m", help="K线周期（默认1m）")
    p.add_argument("--db", default=str(ROOT / "data" / "real_audit.db"), help="审计库路径")
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    print(f"""
================================================================
  真实试运行（paper 撮合） {datetime.now().strftime('%Y-%m-%d %H:%M')}
  标的: {args.symbol} | K线: {args.interval} | 时长: {args.duration}s
  行情: OKX→Binance 主备 | 新闻: Fed RSS + Google News 订阅
================================================================
""")
    knowledge = KnowledgeService()
    audit = AuditLog(args.db)

    bus = EventBus()
    account = Account(base_currency="USDT", cash=INITIAL_CASH)
    account.high_water_mark = INITIAL_CASH
    risk = RiskManager(account)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    analyzer = LLMAnalyzer(knowledge, api_key=api_key or None)
    logger.info("LLM 分析器: %s", "已启用(API)" if analyzer.llm_available else "关键词模式")

    # ---- 信息层：真实 K 线 + 真实新闻 + 真实资金流 ----
    kline_src = RESTKlineSource(symbol=args.symbol, interval=args.interval,
                                exchanges=["okx", "binance"], limit=150)
    flow_src = CryptoFlowSource(symbol=args.symbol)
    news_sources: list = GoogleNewsSource.presets()
    news_sources.append(RSSSource("federal-reserve", KNOWN_FEEDS["federal-reserve"][0],
                                  KNOWN_FEEDS["federal-reserve"][1]))
    news_sources.append(RSSSource("cointelegraph", KNOWN_FEEDS["cointelegraph"][0],
                                  KNOWN_FEEDS["cointelegraph"][1]))
    aggregator = InfoAggregator(
        bus, sources=news_sources, market_data={},
        kline_data={args.symbol: kline_src},
        flow_data={args.symbol: flow_src},
        poll_interval=60.0,           # 新闻 60s 一轮
        kline_poll_interval=8.0,      # K 线 8s 一轮（OKX 限速 20次/2s，余量充足）
        flow_poll_interval=60.0,      # 资金流 60s（OKX taker-volume 为 5m 粒度）
    )

    # ---- 决策层 + 操作层 ----
    strategy = StrategyOrchestrator(
        bus, analyzer, QuantEngine(), risk,
        EconomicCalendar(ROOT / "config" / "economic_calendar.yaml"),
        audit, StrategyConfig(min_decision_interval=5.0))
    strategy.bind()
    detector = BreakoutDetector(bus, BreakoutConfig())
    detector.bind()
    flow_analyzer = FlowAnalyzer(bus, FlowConfig())
    flow_analyzer.bind()

    prices: dict[str, float] = {}
    backfilled: set[str] = set()

    async def price_bridge(ev: KlineEvent) -> None:
        closes = ev.closes()
        if closes:
            prices[ev.symbol] = closes[-1]
        if ev.symbol not in backfilled:      # 首轮历史 K 线回填入库（面板绘图）
            audit.backfill_klines(ev)
            backfilled.add(ev.symbol)

    bus.subscribe("kline", price_bridge)

    broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
    executor = Executor(bus, broker)
    executor.bind()

    # ---- 周期性状态播报 ----
    started = datetime.now(timezone.utc)
    news_count = [0]

    async def status_loop() -> None:
        while True:
            await asyncio.sleep(60)
            eq = account.equity(prices)
            last_px = prices.get(args.symbol, 0.0)
            n_pos = sum(1 for p in account.positions.values() if p.qty > 0)
            logger.info("[状态] %s=%.2f | 净值=%.2f | 持仓标的=%d | 新闻已处理=%d",
                        args.symbol, last_px, eq, n_pos, news_count[0])

    async def _on_news_count(ev) -> None:  # noqa: ANN001
        news_count[0] += 1

    bus.subscribe("news", _on_news_count)

    # ---- 运行与优雅退出 ----
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:   # Windows
            signal.signal(sig, lambda *_: stop.set())

    aggregator.start()
    status_task = asyncio.create_task(status_loop())
    logger.info("系统启动：按 Ctrl+C 或等待 %d 秒后自动停止\n", args.duration)

    try:
        await asyncio.wait_for(stop.wait(), timeout=args.duration)
    except asyncio.TimeoutError:
        logger.info("到达设定时长，自动停止")
    finally:
        aggregator_task = asyncio.create_task(aggregator.stop())
        status_task.cancel()
        await asyncio.gather(aggregator_task, status_task, return_exceptions=True)
        await bus.wait_idle(1.0)
        await kline_src.close()
        await flow_src.close()

    # ---- 汇总 ----
    equity = account.equity(prices)
    dd = risk.update_drawdown(prices)
    decisions = audit.recent_decisions(1000)
    fills = audit.recent_fills(1000)
    runtime = (datetime.now(timezone.utc) - started).total_seconds()
    print("\n" + "=" * 64)
    print(f"真实试运行结束 —— {runtime:.0f} 秒")
    print("=" * 64)
    print(f"  数据源    : K线 {kline_src.active_exchange} | 新闻源 {len(news_sources)} 个")
    print(f"  初始资金  : {INITIAL_CASH:>14,.2f} USDT")
    print(f"  期末净值  : {equity:>14,.2f} USDT ({(equity / INITIAL_CASH - 1) * 100:+.2f}%)")
    print(f"  回撤      : {dd * 100:>13.2f} %  | 熔断: {'是' if account.halted else '否'}")
    print(f"  新闻处理  : {news_count[0]} 条（去重分级后）")
    print(f"  审计      : {len(decisions)} 决策 / {len(fills)} 成交（{args.db}）")
    for p in account.positions.values():
        if p.qty > 0:
            last = prices.get(p.symbol, p.avg_price)
            print(f"  持仓      : {p.symbol} {p.qty:.6f} @ {p.avg_price:.2f}"
                  f" → 浮动 {(last / p.avg_price - 1) * 100:+.2f}%")
    print("=" * 64)
    audit.close()


if __name__ == "__main__":
    asyncio.run(main())
