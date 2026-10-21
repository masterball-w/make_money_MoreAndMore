"""真实试运行入口 —— 真实数据、模拟撮合（paper 模式）。

与 main.py（全 Mock 确定性演示）不同，本入口接入真实世界数据：
  行情: OKX/Binance 公开 REST K 线（无需 API key，主备自动切换）
  新闻: 美联储官网 RSS + Google News 关键词订阅（美联储/中东/央行/AI/加密）
  决策: LLM 新闻分析（config/settings.yaml llm 段配置，默认智谱 GLM；
       未配置密钥时自动降级关键词模式）
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
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.bus import EventBus                              # noqa: E402
from common.events import KlineEvent                         # noqa: E402
from common.llm_config import load_llm_config                # noqa: E402
from common.models import Account                            # noqa: E402
from common.notifier import load_notifier                    # noqa: E402
from decision_layer.audit import AuditLog                    # noqa: E402
from decision_layer.breakout_detector import BreakoutDetector  # noqa: E402
from decision_layer.event_calendar import EconomicCalendar   # noqa: E402
from decision_layer.flow_analyzer import FlowAnalyzer  # noqa: E402
from decision_layer.llm_analyzer import LLMAnalyzer          # noqa: E402
from decision_layer.quant_engine import QuantEngine          # noqa: E402
from decision_layer.risk_manager import RiskManager          # noqa: E402
from decision_layer.runtime_config import load_runtime_config  # noqa: E402
from decision_layer.strategy import StrategyOrchestrator     # noqa: E402
from execution_layer.brokers.paper_broker import PaperBroker  # noqa: E402
from execution_layer.executor import Executor                # noqa: E402
from info_layer.aggregator import InfoAggregator             # noqa: E402
from info_layer.flow import CryptoFlowSource, GateTradesFlowSource  # noqa: E402
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
    p.add_argument("--exchanges", default="okx,binance",
                   help="K线源，逗号分隔 okx|binance|gateio（大陆机房只有 gateio 可达）")
    p.add_argument("--flow-source", default="okx", choices=["okx", "gate", "none"],
                   help="资金流源：okx（taker-volume，默认）| gate（GateIO成交流聚合，大陆机房）| none（禁用）")
    p.add_argument("--no-gnews", action="store_true",
                   help="禁用 Google News 订阅（机房网络不可达时使用，保留 Fed/Cointelegraph RSS）")
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
    limits, strategy_cfg, breakout_cfg, flow_cfg = load_runtime_config(
        ROOT / "config" / "risk_limits.yaml")
    risk = RiskManager(account, limits)

    llm_cfg = load_llm_config(ROOT / "config" / "settings.yaml")
    analyzer = LLMAnalyzer(knowledge, api_key=llm_cfg.api_key, base_url=llm_cfg.base_url,
                           model=llm_cfg.model, timeout=llm_cfg.timeout,
                           extra_body=llm_cfg.extra_body)
    logger.info("LLM 分析器: %s",
                f"已启用({llm_cfg.model} @ {llm_cfg.base_url})" if analyzer.llm_available
                else "关键词模式（未配置密钥）")

    # ---- 信息层：真实 K 线 + 真实新闻 + 真实资金流 ----
    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    # 600 根 1 分钟足够合成 15 分钟趋势（慢线 21 根约需 315 分钟）
    kline_src = RESTKlineSource(symbol=args.symbol, interval=args.interval,
                                exchanges=exchanges, limit=600)
    if args.flow_source == "gate":
        flow_src: CryptoFlowSource | GateTradesFlowSource | None = \
            GateTradesFlowSource(symbol=args.symbol)
    elif args.flow_source == "okx":
        flow_src = CryptoFlowSource(symbol=args.symbol)
    else:
        flow_src = None
    news_sources: list = [] if args.no_gnews else GoogleNewsSource.presets()
    news_sources.append(RSSSource("federal-reserve", KNOWN_FEEDS["federal-reserve"][0],
                                  KNOWN_FEEDS["federal-reserve"][1]))
    news_sources.append(RSSSource("cointelegraph", KNOWN_FEEDS["cointelegraph"][0],
                                  KNOWN_FEEDS["cointelegraph"][1]))
    aggregator = InfoAggregator(
        bus, sources=news_sources, market_data={},
        kline_data={args.symbol: kline_src},
        flow_data={args.symbol: flow_src} if flow_src else {},
        poll_interval=60.0,           # 新闻 60s 一轮
        kline_poll_interval=8.0,      # K 线 8s 一轮（OKX 限速 20次/2s，余量充足）
        flow_poll_interval=60.0,      # 资金流 60s（OKX taker-volume 为 5m 粒度）
    )

    # ---- 决策层 + 操作层 ----
    strategy = StrategyOrchestrator(
        bus, analyzer, QuantEngine(), risk,
        EconomicCalendar(ROOT / "config" / "economic_calendar.yaml"),
        audit, strategy_cfg)
    strategy.bind()
    detector = BreakoutDetector(bus, breakout_cfg)
    detector.bind()
    flow_analyzer = FlowAnalyzer(bus, flow_cfg)
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

    async def _audit_flow(ev) -> None:  # noqa: ANN001
        audit.log_flow(ev)               # 资金流落盘（页面"资金动向"数据源）

    bus.subscribe("flow", _audit_flow)

    broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
    executor = Executor(bus, broker)
    executor.bind()

    # ---- 手机通知桥：成交回报即时推 + 进程启停（未配 webhook 时全部静默跳过）----
    notifier = load_notifier()

    async def _notify_fill(ev) -> None:  # noqa: ANN001
        notifier.notify_fill(ev.symbol, str(ev.side), ev.filled_qty,
                             ev.avg_price, ev.commission)

    bus.subscribe("fill", _notify_fill)
    # 上岗推送带版本信息（config/version.yaml，每次发版时更新）
    from common.version import version_markdown

    notifier.send_markdown(f"🚀 **交易小助手已上岗，开始盯盘**\n\n{version_markdown()}")

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
        notifier.notify_lifecycle("停止")
        await kline_src.close()
        if flow_src:
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
