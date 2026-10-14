"""策略参数回测对比 —— 用审计库真实 K 线/资金流回放新旧两套参数。

与 backtest/replay.py 的区别（在其思想上的增强版）：
  1. 接入了 BreakoutDetector + FlowAnalyzer（回放引擎原本只喂行情，突破/资金流
     信号缺失，会严重低估策略行为）
  2. 事件时钟注入（strategy._clock_override）——信号 TTL/评估限流/时间止损
     在回测中按事件时间推进，而非墙钟

用法（服务器上）:
    .venv/bin/python scripts/backtest_compare.py --db data/audit.db
对比对象:
    旧参数 = 原版（LLM 0.40 权重、止损 5%、无止盈/时间止损）
    新参数 = 日内短线版（LLM 0.20、突破 0.55/资金流 0.50、止损 1.2%、
             止盈 1.5%、30 分钟超时离场）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from backtest.replay import BacktestRunner  # noqa: E402
from common.bus import EventBus  # noqa: E402
from common.events import Candle, FlowEvent, KlineEvent, Market  # noqa: E402
from decision_layer.audit import AuditLog  # noqa: E402
from decision_layer.breakout_detector import BreakoutConfig, BreakoutDetector  # noqa: E402
from decision_layer.flow_analyzer import FlowAnalyzer, FlowConfig  # noqa: E402
from decision_layer.strategy import StrategyConfig  # noqa: E402

OLD_CFG = StrategyConfig(          # 原版参数（2026-10-12 部署时的默认值）
    w_llm=0.40, w_quant=0.20, w_breakout=0.45, w_flow=0.40,
    stop_loss_pct=0.05, take_profit_pct=None, time_stop_minutes=None,
    min_decision_interval=0.0)
NEW_CFG = StrategyConfig(          # 日内短线版（现默认值，显式列出便于对照）
    w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
    stop_loss_pct=0.012, take_profit_pct=0.015, time_stop_minutes=30,
    min_decision_interval=0.0)


def load_events(db_path: str, symbol: str = "BTC/USDT") -> list:
    """审计库 → 事件流（KlineEvent 带滚动历史 + FlowEvent）。"""
    audit = AuditLog(db_path)
    try:
        klines = sorted(audit.recent_klines(symbol, 100000), key=lambda k: k["ts"])
        flows = audit.recent_flows(symbol, 100000)
    finally:
        audit.close()

    def parse(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    events: list = []
    candles = [Candle(ts=parse(k["ts"]), open=float(k["open"]), high=float(k["high"]),
                      low=float(k["low"]), close=float(k["close"]),
                      volume=float(k["volume"]), closed=True) for k in klines]
    for i, c in enumerate(candles):
        events.append(KlineEvent(ts=c.ts, symbol=symbol, market=Market.CRYPTO,
                                 interval="1m",
                                 current=c, history=candles[max(0, i - 150):i]))
    for f in flows:
        events.append(FlowEvent(symbol=symbol, market=Market.CRYPTO,
                                buy_volume=float(f["buy_volume"]),
                                sell_volume=float(f["sell_volume"]),
                                net_flow=float(f["net_flow"]),
                                total_volume=float(f["total_volume"]),
                                period=str(f["period"]), ts=parse(f["ts"])))
    events.sort(key=lambda e: e.ts)
    return events


async def run_one(events: list, cfg: StrategyConfig, name: str, audit_path: str) -> dict:
    Path(audit_path).unlink(missing_ok=True)
    runner = BacktestRunner(events, strategy_config=cfg, audit_path=audit_path)
    # 回放引擎缺失的接线：突破检测器 + 资金流分析器（同总线，实时产信号）
    detector = BreakoutDetector(runner.bus, BreakoutConfig())
    detector.bind()
    flow_analyzer = FlowAnalyzer(runner.bus, FlowConfig())
    flow_analyzer.bind()

    runner.strategy.bind()
    runner.executor.bind()
    prices = runner._prices
    for event in events:
        # 事件时钟注入（策略 TTL/限流、风控 R5 冷却、资金流冷却去抖）
        runner.strategy._clock_override = event.ts
        runner.risk._clock_override = event.ts
        flow_analyzer._clock_override = event.ts
        if isinstance(event, KlineEvent) and event.closes():
            prices[event.symbol] = event.closes()[-1]
        await runner.bus.publish(event)
        await asyncio.sleep(0)

    result = runner.result
    result.n_decisions = 0
    audit = AuditLog(audit_path)
    try:
        result.n_decisions = len(audit.recent_decisions(100000))
        result.n_fills = len(audit.recent_fills(100000))
        fills = audit.recent_fills(200)
    finally:
        audit.close()
    result.final_equity = runner.account.equity(prices)
    summary = result.summary()
    # 意图与拦截统计：区分"从没想开仓"和"想开但被风控拦"
    audit2 = AuditLog(audit_path)
    try:
        ds = audit2.recent_decisions(100000)
    finally:
        audit2.close()
    intent_buy = sum(1 for d in ds if str(d["action"]).upper() == "BUY")
    intent_sell = sum(1 for d in ds if str(d["action"]).upper() == "SELL")
    blocked = sum(1 for d in ds if d["blocked_by_risk"])
    # 拦截原因分布
    import json as _json
    from collections import Counter as _Counter
    fail_reasons = _Counter()
    for d in ds:
        if d["blocked_by_risk"]:
            try:
                for c in _json.loads(d["risk_checks"] or "[]"):
                    if not c.get("passed", True):
                        fail_reasons[f"{c.get('name')}: {str(c.get('detail'))[:40]}"] += 1
            except Exception:  # noqa: BLE001
                pass
    print(f"\n===== {name} =====")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"  意图开仓(BUY): {intent_buy} | 意图平仓(SELL): {intent_sell} | 被风控拦截: {blocked}")
    for r, n in fail_reasons.most_common(5):
        print(f"    拦截原因 {n:5d} × {r}")
    # 样本：按插入顺序（=回放时间序）取最早的决策与成交
    import sqlite3 as _sq
    conn = _sq.connect(audit_path)
    rows = conn.execute(
        "SELECT rowid, action, quantity, blocked_by_risk, reason FROM decisions ORDER BY rowid"
    ).fetchall()
    conn.close()
    for r in rows[:6]:
        print(f"    [首{r[0]}] {str(r[1]):5s} qty={r[2]:.6g} blocked={r[3]} {str(r[4])[:52]}")
    conn = _sq.connect(audit_path)
    frows = conn.execute(
        "SELECT rowid, ts, side, filled_qty, avg_price FROM fills ORDER BY rowid"
    ).fetchall()
    conn.close()
    print(f"  fills 按插入序({len(frows)}笔):")
    for r in frows[:12]:
        print(f"    #{r[0]} {str(r[1])[5:19]} {r[2]:4s} {r[3]:.4f} @ {r[4]:,.0f}")
    print("  成交流水(最近8笔):")
    for f in fills[:8]:
        ts = str(f["ts"])[5:16]
        print(f"    {ts} {f['side']:4s} {float(f['filled_qty']):.4f} @ {float(f['avg_price']):,.0f}")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="新旧策略参数回测对比")
    p.add_argument("--db", default=str(ROOT / "data" / "audit.db"))
    p.add_argument("--symbol", default="BTC/USDT")
    args = p.parse_args()

    events = load_events(args.db, args.symbol)
    k_n = sum(1 for e in events if isinstance(e, KlineEvent))
    f_n = sum(1 for e in events if isinstance(e, FlowEvent))
    if k_n < 50:
        print(f"K线太少({k_n}根)，不足以回测")
        return 1
    print(f"回测素材: {k_n} 根K线 + {f_n} 期资金流"
          f"（{events[0].ts:%m-%d %H:%M} ~ {events[-1].ts:%m-%d %H:%M} UTC）")

    asyncio.run(run_one(events, OLD_CFG, "A. 旧参数（LLM权重0.40/止损5%/无止盈）",
                         "data/bt_a.db"))
    asyncio.run(run_one(events, NEW_CFG, "B. 新参数（日内版：降LLM/紧止损/止盈/超时）",
                         "data/bt_b.db"))
    # C. 敏感度实验：B + 降低入场门槛（回测无新闻输入且只有收盘K线，
    #    信号强度天然低于实盘，用 0.25 门槛检验短线规则的期望值方向）
    AGGR_CFG = StrategyConfig(
        w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
        entry_threshold=0.25,
        stop_loss_pct=0.012, take_profit_pct=0.015, time_stop_minutes=30,
        min_decision_interval=0.0)
    asyncio.run(run_one(events, AGGR_CFG, "C. B + 入场门槛0.35→0.25",
                         "data/bt_c.db"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
