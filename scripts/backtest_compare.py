"""策略参数回测对比 —— 用审计库真实 K 线/资金流回放多套参数。

变体（对照部署阶段）：
  V0 服务器现行版   无趋势过滤/跟单，TP1.5/SL1.2/超时30
  V1 趋势+跟单      做多须顺 15m 趋势；跟随大额流入；TP1.5/SL1.2/无超时
  V2 +果断三段式    +0.4%保本锁定/+0.7%跟踪止盈/1.0%硬顶，SL0.7/超时20
  V3 +震荡收割      下轨超卖接、上轨超买卖

数据说明：GateIO 字段序修复(2026-10)之前入库的 K 线 open/close 两列是颠倒的，
load_events(swap_oc=True) 按真实语义还原（真开盘在 close 列，真收盘在 open 列）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from backtest.replay import BacktestRunner  # noqa: E402
from common.events import Candle, FlowEvent, KlineEvent, Market  # noqa: E402
from decision_layer.audit import AuditLog  # noqa: E402
from decision_layer.breakout_detector import BreakoutConfig, BreakoutDetector  # noqa: E402
from decision_layer.flow_analyzer import FlowAnalyzer, FlowConfig  # noqa: E402
from decision_layer.strategy import StrategyConfig  # noqa: E402

HIST = 420          # 15 分钟趋势需要 21 根 15m ≈ 315 根 1m，留余量

V0 = StrategyConfig(  # 服务器现行（2026-10-14 部署的默认值）
    w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
    stop_loss_pct=0.012, take_profit_pct=0.015, time_stop_minutes=30,
    trend_filter=False, follow_flow=False,
    min_decision_interval=0.0)
V1 = StrategyConfig(  # 趋势过滤 + 跟单（用户 risk_limits.yaml 方案）
    w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
    stop_loss_pct=0.012, take_profit_pct=0.015, time_stop_minutes=None,
    trend_filter=True, follow_flow=True,
    min_decision_interval=0.0)
V2 = StrategyConfig(  # V1 + 果断三段式离场
    w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
    stop_loss_pct=0.007, take_profit_pct=0.010, time_stop_minutes=20,
    trend_filter=True, follow_flow=True,
    breakeven_at=0.004, breakeven_lock=0.0015,
    trail_at=0.007, trail_gap=0.0025,
    min_decision_interval=0.0)
V3 = StrategyConfig(  # V2 + 震荡收割
    w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
    stop_loss_pct=0.007, take_profit_pct=0.010, time_stop_minutes=20,
    trend_filter=True, follow_flow=True,
    breakeven_at=0.004, breakeven_lock=0.0015,
    trail_at=0.007, trail_gap=0.0025,
    mr_enable=True,
    min_decision_interval=0.0)


def load_events(db_path: str, symbol: str = "BTC/USDT",
                swap_oc: bool = True) -> list:
    """审计库 → 事件流。swap_oc=True 用于 GateIO 字段序修复前的历史数据。"""
    audit = AuditLog(db_path)
    try:
        klines = sorted(audit.recent_klines(symbol, 100000), key=lambda k: k["ts"])
        flows = audit.recent_flows(symbol, 100000)
    finally:
        audit.close()

    def parse(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    candles = []
    for k in klines:
        o, c = float(k["open"]), float(k["close"])
        if swap_oc:                      # 修复前 open 列存的其实是收盘价
            o, c = c, o
        candles.append(Candle(ts=parse(k["ts"]), open=o, high=float(k["high"]),
                              low=float(k["low"]), close=c,
                              volume=float(k["volume"]), closed=True))
    events: list = []
    for i, c in enumerate(candles):
        events.append(KlineEvent(ts=c.ts, symbol=symbol, market=Market.CRYPTO,
                                 interval="1m", current=c,
                                 history=candles[max(0, i - HIST):i]))
    for f in flows:
        events.append(FlowEvent(symbol=symbol, market=Market.CRYPTO,
                                buy_volume=float(f["buy_volume"]),
                                sell_volume=float(f["sell_volume"]),
                                net_flow=float(f["net_flow"]),
                                total_volume=float(f["total_volume"]),
                                period=str(f["period"]), ts=parse(f["ts"])))
    events.sort(key=lambda e: e.ts)
    return events


async def run_one(events: list, cfg: StrategyConfig, name: str,
                  audit_path: str) -> dict:
    Path(audit_path).unlink(missing_ok=True)
    runner = BacktestRunner(events, strategy_config=cfg, audit_path=audit_path)
    detector = BreakoutDetector(runner.bus, BreakoutConfig())
    detector.bind()
    flow_analyzer = FlowAnalyzer(runner.bus, FlowConfig())
    flow_analyzer.bind()
    runner.strategy.bind()
    runner.executor.bind()

    prices = runner._prices
    for event in events:
        runner.strategy._clock_override = event.ts
        runner.risk._clock_override = event.ts
        flow_analyzer._clock_override = event.ts
        if isinstance(event, KlineEvent) and event.closes():
            prices[event.symbol] = event.closes()[-1]
        await runner.bus.publish(event)
        await asyncio.sleep(0)

    equity = runner.account.equity(prices)
    audit = AuditLog(audit_path)
    try:
        ds = audit.recent_decisions(100000)
        fills = audit.recent_fills(100000)
    finally:
        audit.close()
    runner.audit.close()

    acts = Counter((str(d["action"]).upper(), bool(d["blocked_by_risk"])) for d in ds)
    fees = sum(float(f["commission"] or 0) for f in fills)
    exit_kinds = Counter()
    for d in ds:
        if str(d["action"]).lower() == "sell" and not d["blocked_by_risk"]:
            r = str(d["reason"] or "")
            for kw in ("跟踪止盈落袋", "保本离场", "止盈落袋", "止损触发", "超时离场",
                       "震荡收割", "清仓离场"):
                if kw in r:
                    exit_kinds[kw] += 1
                    break
    print(f"\n===== {name} =====")
    print(f"  期末净值: {equity:,.2f}（{(equity/100000-1)*100:+.2f}%）| "
          f"成交 {len(fills)} 笔 | 手续费 {fees:.2f}")
    print(f"  意图: 买{acts.get(('BUY', False),0)}/{acts.get(('BUY',True),0)}拦 "
          f"卖{acts.get(('SELL',False),0)}/{acts.get(('SELL',True),0)}拦 | 决策 {len(ds)}")
    if exit_kinds:
        print("  离场方式: " + "，".join(f"{k}×{v}" for k, v in exit_kinds.most_common()))
    return {"name": name, "equity": equity, "fills": len(fills), "fees": fees}


def main() -> int:
    p = argparse.ArgumentParser(description="多策略参数回测对比")
    p.add_argument("--db", default=str(ROOT / "data" / "audit.db"))
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--no-swap", action="store_true",
                   help="数据已是字段序修复后的（不还原 open/close）")
    args = p.parse_args()

    events = load_events(args.db, args.symbol, swap_oc=not args.no_swap)
    k_n = sum(1 for e in events if isinstance(e, KlineEvent))
    f_n = sum(1 for e in events if isinstance(e, FlowEvent))
    if k_n < 300:
        print(f"K线太少({k_n}根)，不足以回测")
        return 1
    print(f"回测素材: {k_n} 根K线 + {f_n} 期资金流"
          f"（{events[0].ts:%m-%d %H:%M} ~ {events[-1].ts:%m-%d %H:%M} UTC）")

    asyncio.run(run_one(events, V0, "V0 服务器现行（TP1.5/SL1.2/超时30）", "data/bt_v0.db"))
    asyncio.run(run_one(events, V1, "V1 趋势过滤+跟单（TP1.5/SL1.2/无超时）", "data/bt_v1.db"))
    asyncio.run(run_one(events, V2, "V2 +果断三段式（保本+跟踪/SL0.7/超时20）", "data/bt_v2.db"))
    asyncio.run(run_one(events, V3, "V3 +震荡收割（下轨接/上轨抛）", "data/bt_v3.db"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
