"""离场参数扫描 —— breakeven_at × trail_at × time_stop 组合在九天数据上的对比。

背景（2026-10-21 实测）：最近10笔回合持仓期内最大浮盈 +0.03%~+0.38%，
无一触发 0.4% 保本门槛；出场后1小时市场平均再走 +0.4%。怀疑门槛/窗口
的时间尺度与波动节奏错配。本脚本用九天回测数据裁决。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from backtest.replay import BacktestRunner  # noqa: E402
from backtest_compare import V2M, load_events  # noqa: E402
from collections import Counter  # noqa: E402
from decision_layer.audit import AuditLog  # noqa: E402
from decision_layer.breakout_detector import BreakoutConfig, BreakoutDetector  # noqa: E402
from decision_layer.flow_analyzer import FlowAnalyzer, FlowConfig  # noqa: E402
from decision_layer.strategy import StrategyConfig  # noqa: E402
from common.events import KlineEvent  # noqa: E402

# (名称, breakeven_at, trail_at, trail_gap, time_stop)
COMBOS = [
    ("现行 be0.4/tr0.7/t20", 0.004, 0.007, 0.0025, 20),
    ("早锁 be0.2/tr0.4/t20", 0.002, 0.004, 0.0025, 20),
    ("耐心 be0.4/tr0.7/t60", 0.004, 0.007, 0.0025, 60),
    ("早锁+耐心 be0.2/tr0.4/t40", 0.002, 0.004, 0.0025, 40),
    ("中庸 be0.3/tr0.5/t40", 0.003, 0.005, 0.0025, 40),
    ("超长窗 be0.2/tr0.6/t90", 0.002, 0.006, 0.0030, 90),
]


def cfg_of(be, tr, gap, tmin):
    return StrategyConfig(
        w_llm=0.20, w_quant=0.25, w_breakout=0.55, w_flow=0.50,
        stop_loss_pct=0.007, take_profit_pct=0.010, time_stop_minutes=tmin,
        trend_filter=True, follow_flow=True,
        breakeven_at=be, breakeven_lock=0.0015,
        trail_at=tr, trail_gap=gap,
        maker_entries=True, min_decision_interval=0.0)


async def run(events, cfg, path):
    Path(path).unlink(missing_ok=True)
    runner = BacktestRunner(events, strategy_config=cfg, audit_path=path)
    BreakoutDetector(runner.bus, BreakoutConfig()).bind()
    FlowAnalyzer(runner.bus, FlowConfig()).bind()
    runner.strategy.bind()
    runner.executor.bind()
    prices = runner._prices
    for ev in events:
        runner.strategy._clock_override = ev.ts
        runner.risk._clock_override = ev.ts
        if isinstance(ev, KlineEvent) and ev.closes():
            prices[ev.symbol] = ev.closes()[-1]
        await runner.bus.publish(ev)
        await asyncio.sleep(0)
    eq = runner.account.equity(prices)
    audit = AuditLog(path)
    try:
        fills = audit.recent_fills(100000)
        ds = audit.recent_decisions(100000)
    finally:
        audit.close()
    runner.audit.close()
    fees = sum(float(f["commission"] or 0) for f in fills)
    exits = Counter()
    for d in ds:
        if str(d["action"]).lower() == "sell" and not d["blocked_by_risk"]:
            r = str(d["reason"] or "")
            for kw in ("跟踪止盈落袋", "保本离场", "止盈落袋", "止损触发", "超时离场", "清仓离场"):
                if kw in r:
                    exits[kw] += 1
                    break
    return eq, len(fills), fees, exits


def main() -> int:
    events = load_events(sys.argv[1] if len(sys.argv) > 1 else "D:/tmp_bt/audit.db")
    print(f"扫描素材: {len(events)} 事件")
    results = []
    for name, be, tr, gap, tmin in COMBOS:
        eq, n, fees, exits = asyncio.run(run(events, cfg_of(be, tr, gap, tmin),
                                             "data/sweep_tmp.db"))
        gross = 100000 - eq + fees
        exit_s = "，".join(f"{k.replace('离场','').replace('落袋','')}×{v}"
                          for k, v in exits.most_common(4))
        print(f"{name:24s} 净值{eq:,.0f} ({(eq/1000-100):+.2f}%) | {n}笔 费{fees:.0f} "
              f"毛利{gross:+.0f} | {exit_s}", flush=True)
        results.append((name, eq))
    best = max(results, key=lambda x: x[1])
    print(f"\n最优: {best[0]} -> {best[1]:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
