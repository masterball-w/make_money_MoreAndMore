"""阶段1演示入口 —— 模拟盘跑通四层全链路（含实时 K 线 + 指标突破触发）。

剧本（完全确定性）：
  Phase A   K 线预热：MockKlineData 推送 OHLCV K 线流（含盘中 tick 更新），
            突破检测器建立指标状态（MACD/布林/Donchian 通道）
  Phase B   P1 利好新闻："美联储意外降息50bp" → LLM 情绪 → 信号融合 → 买入
  Phase C   重复新闻注入 → 信息层指纹去重拦截
  Phase D   P1 利空新闻 → 信号不足 → 按兵不动（保守）
  Phase D2  注入闪崩 K 线 → 跌破 5% 止损线 → 无条件清仓
  Phase E   P2 正面新闻刷新情绪 → 连续注入 4 根大阳线 → K 线突破
            Donchian 高点/布林上轨 → 突破检测器产出 SignalEvent
            → 决策层强制评估 → 技术突破驱动买入
  结束      打印账户净值/持仓/审计统计

运行:  python main.py          （paper 模式，默认）
       设置 OPENAI_API_KEY 可启用真实 LLM 分析（可选，自动降级）
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.bus import EventBus                      # noqa: E402
from common.events import Market, NewsCategory       # noqa: E402
from common.models import Account                    # noqa: E402
from decision_layer.audit import AuditLog            # noqa: E402
from decision_layer.breakout_detector import BreakoutDetector, BreakoutConfig  # noqa: E402
from decision_layer.event_calendar import EconomicCalendar  # noqa: E402
from decision_layer.llm_analyzer import LLMAnalyzer  # noqa: E402
from decision_layer.quant_engine import QuantEngine  # noqa: E402
from decision_layer.risk_manager import RiskLimits, RiskManager  # noqa: E402
from decision_layer.strategy import StrategyConfig, StrategyOrchestrator  # noqa: E402
from execution_layer.brokers.paper_broker import PaperBroker  # noqa: E402
from execution_layer.executor import Executor        # noqa: E402
from info_layer.aggregator import InfoAggregator     # noqa: E402
from info_layer.base import RawNews                  # noqa: E402
from info_layer.kline import MockKlineData           # noqa: E402
from knowledge_layer.knowledge_service import KnowledgeService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

BANNER = """
==============================================================
  四层自动化交易系统 · 模拟盘全链路演示 (paper mode)
  信息层(新闻+实时K线) → 决策层(指标突破检测+风控门禁+审计)
       → 操作层(模拟撮合) → 成交回流
==============================================================
"""

SYMBOL = "BTC/USDT"
INITIAL_CASH = 100_000.0


def _live_guard() -> None:
    """live 模式双保险：配置显式声明 + 环境变量二次确认。"""
    mode = os.environ.get("TRADING_MODE", "paper")
    if mode == "live" and os.environ.get("TRADING_LIVE_CONFIRM") != "YES":
        raise SystemExit(
            "[安全退出] live 模式需要同时设置 TRADING_MODE=live 和 TRADING_LIVE_CONFIRM=YES。"
            "请先完成回测验证与模拟盘观察。")


async def run_demo() -> None:
    _live_guard()
    print(BANNER)
    knowledge = KnowledgeService()
    audit = AuditLog(ROOT / "data" / "audit.db")

    # ---- 组件装配（四层全部通过总线解耦） ----
    bus = EventBus()
    account = Account(base_currency="USDT", cash=INITIAL_CASH)
    risk = RiskManager(account, RiskLimits(cooldown_minutes=0))   # 演示关冷却期

    api_key = os.environ.get("OPENAI_API_KEY", "")
    analyzer = LLMAnalyzer(knowledge, api_key=api_key or None)
    logger.info("LLM 分析器: %s", "已启用(API)" if analyzer.llm_available else "关键词回退模式(未配置 OPENAI_API_KEY)")

    klines = MockKlineData(symbol=SYMBOL, start_price=50_000.0, drift=0.0002,
                           volatility=0.0015, seed=42, ticks_per_candle=4)
    strategy = StrategyOrchestrator(
        bus, analyzer, QuantEngine(), risk,
        EconomicCalendar(ROOT / "config" / "economic_calendar.yaml"),
        audit, StrategyConfig(min_decision_interval=0.3))
    strategy.bind()
    detector = BreakoutDetector(bus, BreakoutConfig())
    detector.bind()

    prices: dict[str, float] = {}
    broker = PaperBroker(price_provider=lambda s: prices.get(s, 0.0))
    executor = Executor(bus, broker)
    executor.bind()

    aggregator = InfoAggregator(bus, sources=[], market_data={})
    logger.info("四层装配完成：信息层(新闻+K线)/决策层(突破检测+风控)/操作层/知识层 全部就绪\n")

    async def push_kline() -> float:
        """推一次 K 线流更新，返回最新价。"""
        ev = await klines.next(SYMBOL)
        prices[SYMBOL] = ev.closes()[-1]
        await aggregator.push_kline(ev)
        return ev.closes()[-1]

    # ---- Phase A: K 线预热（指标状态建立） ----
    logger.info("---- Phase A: 实时K线流预热（16 次 tick 更新，指标状态建立）----")
    for i in range(16):
        p = await push_kline()
        cur = "当前K线形成中" if klines.current is not None else "K线刚收盘"
        logger.info("[K线] #%02d BTC/USDT close=%.2f (%s)", i + 1, p, cur)
        await asyncio.sleep(0.02)
    await bus.wait_idle(0.3)

    # ---- Phase B: P1 利好新闻 → 全链路买入 ----
    logger.info("\n---- Phase B: P1 利好新闻进入信息层 ----")
    good = RawNews(
        title="美联储宣布紧急降息50个基点，重启量化宽松",
        body="美联储主席表示将无条件提供流动性支持，风险资产应声大涨。",
        source="mock-wire", category=NewsCategory.FED)
    await aggregator.ingest_raw(good)
    await bus.wait_idle(0.5)

    # ---- Phase C: 重复新闻 → 去重 ----
    logger.info("\n---- Phase C: 重复新闻注入（验证去重）----")
    dup = RawNews(
        title="美联储宣布紧急降息50个基点，重启量化宽松！！",   # 标点差异不影响指纹
        body="转载：美联储降息。",
        source="repost", category=NewsCategory.FED)
    await aggregator.ingest_raw(dup)
    await bus.wait_idle(0.3)

    # ---- Phase D: 利空新闻 → 信号不足按兵不动 ----
    logger.info("\n---- Phase D: P1 利空新闻进入信息层 ----")
    bad = RawNews(
        title="中东局势升级：多地遭导弹袭击，全球进入避险模式",
        body="避险资产大涨，加密市场遭遇流动性冲击短线急跌。",
        source="mock-wire", category=NewsCategory.GEO)
    await aggregator.ingest_raw(bad)
    await bus.wait_idle(0.5)

    # ---- Phase D2: 闪崩 K 线 → 止损保护 ----
    pos = account.position(SYMBOL, Market.CRYPTO)
    if pos.qty > 0:
        logger.info("\n---- Phase D2: 注入闪崩 K 线 → 检验止损保护 ----")
        klines.inject_candle(round(pos.avg_price * 0.92, 2))    # -8%，击穿 5% 止损线
        p = await push_kline()
        logger.info("[K线] 闪崩注入 close=%.2f", p)
        await bus.wait_idle(0.5)

    # ---- Phase E: K 线突破 → 技术指标触发操作 ----
    logger.info("\n---- Phase E1: P2 正面新闻刷新情绪缓存 ----")
    await aggregator.ingest_raw(RawNews(
        title="G20财长会议批准加密资产合规发展框架",
        body="监管明朗化提振市场风险偏好。",
        source="mock-wire", category=NewsCategory.CRYPTO))
    await bus.wait_idle(0.3)

    logger.info("---- Phase E2: 连续注入 4 根大阳线 → 实时K线突破技术指标 ----")
    base_price = prices[SYMBOL]
    for i in range(4):
        target = round(base_price * (1.04 ** (i + 1)), 2)       # 每根 +4%
        klines.inject_candle(target)
        p = await push_kline()
        logger.info("[K线] 大阳线 #%d close=%.2f", i + 1, p)
        await bus.wait_idle(0.3)

    # 收尾再推几根正常 K 线
    for _ in range(4):
        await push_kline()
        await asyncio.sleep(0.02)
    await bus.wait_idle(0.3)

    # ---- 汇总 ----
    equity = account.equity(prices)
    dd = risk.update_drawdown(prices)
    print("\n" + "=" * 62)
    print("演示结束 —— 账户汇总")
    print("=" * 62)
    print(f"  初始资金 : {INITIAL_CASH:>14,.2f} USDT")
    print(f"  期末净值 : {equity:>14,.2f} USDT")
    print(f"  累计盈亏 : {equity - INITIAL_CASH:>+14,.2f} USDT "
          f"({(equity / INITIAL_CASH - 1) * 100:+.2f}%)")
    print(f"  当前回撤 : {dd * 100:>13.2f} %   熔断状态: {'是' if account.halted else '否'}")
    for p_ in account.positions.values():
        if p_.qty > 0:
            last = prices.get(p_.symbol, p_.avg_price)
            mv = p_.market_value(last)
            pnl = (last / p_.avg_price - 1) * 100 if p_.avg_price else 0
            print(f"  持仓     : {p_.symbol} {p_.qty:.6f} @ {p_.avg_price:.2f} "
                  f"→ 市值 {mv:,.2f} ({pnl:+.2f}%)")
    decisions = audit.recent_decisions(500)
    fills = audit.recent_fills(500)
    print(f"  审计记录 : {len(decisions)} 条决策 / {len(fills)} 笔成交"
          f"（含风控检查明细，库: data/audit.db）")
    print("=" * 62)
    audit.close()


if __name__ == "__main__":
    asyncio.run(run_demo())
