"""交易系统日报/快照 —— 用户友好表述版。

数据源：data/audit.db（决策/成交/K线/资金流）+ 按成交记录重建的账户状态。

话术原则（普通人能懂）：
  决策      → 小助手的判断（想买 / 想卖 / 再等等）
  置信度    → 出手意愿（强 / 中等 / 谨慎）
  LLM 情绪  → 新闻氛围（偏乐观 / 中性 / 偏悲观）
  K线新鲜度 → 行情数据 X 分钟前更新
用法:
    python scripts/daily_report.py                 # 预览
    python scripts/daily_report.py --send          # 推送飞书
    python scripts/daily_report.py --snapshot      # 实时快照（1小时窗口）
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from common.notifier import load_notifier  # noqa: E402
from decision_layer.audit import AuditLog  # noqa: E402

DB_PATH = ROOT / "data" / "audit.db"
ASSET = "SPY"                    # 虚拟盘标的（美股时代）
CURRENCY = "USD"
_COIN_NAME = {"BTC": "比特币", "ETH": "以太坊", "SPY": "标普500ETF(SPY)"}
INITIAL_CASH = 100_000.0


def _parse_ts(ts: str) -> datetime:
    """审计库时间戳（ISO 字符串，可能带/不带时区）→ 统一 UTC。"""
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def account_state(audit: AuditLog | None = None, symbol: str = ASSET) -> dict:
    """按审计成交记录重建账户（跨进程拿不到监控进程内存态，成交表是唯一事实源）。"""
    state = {"cash": INITIAL_CASH, "positions": [], "last_price": 0.0,
             "equity": INITIAL_CASH, "pnl": 0.0, "pnl_pct": 0.0, "n_buy": 0, "n_sell": 0}
    if audit is None:
        if not DB_PATH.exists():
            return state
        audit = AuditLog(str(DB_PATH))
        try:
            return account_state(audit, symbol)
        finally:
            audit.close()
    fills = sorted(audit.recent_fills(5000), key=lambda f: str(f["ts"]))
    klines = sorted(audit.recent_klines(symbol, 1500), key=lambda k: k["ts"])
    last_price = float(klines[-1]["close"]) if klines else 0.0
    qty_by: dict[str, float] = defaultdict(float)
    cost_by: dict[str, float] = defaultdict(float)
    for f in fills:
        q, p = float(f["filled_qty"]), float(f["avg_price"])
        sym = str(f["symbol"])
        fee = float(f["commission"] or 0)
        if "BUY" in str(f["side"]).upper():
            qty_by[sym] += q
            cost_by[sym] += q * p
            state["cash"] -= q * p + fee
            state["n_buy"] += 1
        else:
            held = qty_by[sym]
            if held > 0:
                cost_by[sym] -= (cost_by[sym] / held) * min(q, held)
            qty_by[sym] -= q
            state["cash"] += q * p - fee
            state["n_sell"] += 1
    positions, mkt = [], 0.0
    for sym, qty in qty_by.items():
        if qty > 1e-9:
            avg = cost_by[sym] / qty
            last = last_price if sym == symbol else 0.0
            mkt += qty * last
            positions.append({"symbol": sym, "qty": round(qty, 8),
                              "avg_price": round(avg, 2), "last_price": last,
                              "pnl_pct": round((last / avg - 1) * 100, 2) if avg > 0 and last > 0 else 0.0})
    state.update(positions=positions, last_price=last_price,
                 equity=state["cash"] + mkt, n_pos=len(positions))
    state["pnl"] = state["equity"] - INITIAL_CASH
    state["pnl_pct"] = (state["equity"] / INITIAL_CASH - 1) * 100
    return state


def _coin(symbol: str) -> str:
    return _COIN_NAME.get(symbol.split("/")[0], symbol.split("/")[0])


def _mood(v: float) -> str:
    if v >= 0.25:
        return "偏乐观 😊"
    if v <= -0.25:
        return "偏悲观 😟"
    return "偏中性 😐"


def _will(v: float) -> str:
    if v >= 0.6:
        return "强（看好就果断出手）"
    if v >= 0.35:
        return "中等（机会像样才动手）"
    return "谨慎（没把握就先不动）"


def _chg_word(v: float) -> str:
    if v > 0.05:
        return f"涨了 {abs(v):.2f}%"
    if v < -0.05:
        return f"跌了 {abs(v):.2f}%"
    return "基本没变"


def build_report(audit: AuditLog, symbol: str, hours: int,
                 title: str = "") -> str:
    now_local = datetime.now().strftime("%m月%d日 %H:%M")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    window = "1 小时" if hours <= 2 else f"{hours} 小时"

    decisions = [d for d in audit.recent_decisions(1000) if _parse_ts(d["ts"]) >= since]
    fills = [f for f in audit.recent_fills(1000) if _parse_ts(f["ts"]) >= since]
    klines = sorted(audit.recent_klines(symbol, 1500), key=lambda k: k["ts"])

    if not any([decisions, fills, klines]):
        header = title or "📈 **美股趋势小助手 · 每日战报**"
        return f"{header}\n{now_local}\n\n小助手刚上岗，还在熟悉市场，稍后再来看看吧"

    acct = account_state(audit, symbol)
    coin = _coin(symbol)
    L = [title or "📈 **美股趋势小助手 · 每日战报**", now_local, ""]

    # ---- 我的资产 ----
    mkt = acct["equity"] - acct["cash"]
    pnl_word = (f"赚了 {acct['pnl']:+,.0f} USD" if acct["pnl"] >= 0
                else f"亏了 {abs(acct['pnl']):,.0f} USD")
    L += ["💰 **我的资产**",
          f"总资产 **{acct['equity']:,.0f} USD**（现金 {acct['cash']:,.0f}"
          + (f" + 币值 {mkt:,.0f}" if mkt > 0.5 else "") + "）",
          f"开局至今{pnl_word}（{acct['pnl_pct']:+.2f}%）", ""]

    # ---- 行情 ----
    if klines:
        price = float(klines[-1]["close"])
        # 日线节奏：以上一根收盘为涨跌基准
        prev_close = float(klines[-2]["close"]) if len(klines) >= 2 else price
        chg = (price / prev_close - 1) * 100 if prev_close else 0.0
        L += ["📈 **行情**", f"{coin} 收盘 **{price:,.2f} USD**，"
              f"比上一交易日{_chg_word(chg)}", ""]

    # ---- 小助手做了什么 ----
    if decisions:
        acts = Counter(str(d.get("action", "")).upper().rsplit(".", 1)[-1] for d in decisions)
        did = (f"实际动手 {len(fills)} 次"
               + (f"（买入 {sum(1 for f in fills if 'BUY' in str(f['side']).upper())}"
                  f" / 卖出 {sum(1 for f in fills if 'SELL' in str(f['side']).upper())}）"
                  if fills else "，都在观望没出手"))
        L += [f"🤖 **小助手这 {window}**",
              f"做了 {len(decisions)} 次判断：{acts.get('BUY', 0)} 次想买、"
              f"{acts.get('SELL', 0)} 次想卖、{acts.get('HOLD', 0)} 次选择再等等",
              did]
        if fills:
            fee = sum(float(f["commission"] or 0) for f in fills)
            f0 = fills[0]                                   # recent_fills 新→旧
            val = float(f0["filled_qty"]) * float(f0["avg_price"])
            side = "买入" if "BUY" in str(f0["side"]).upper() else "卖出"
            L.append(f"手续费共 {fee:.2f} USD；最近一笔：{_parse_ts(f0['ts']):%H:%M} "
                     f"{side} {float(f0['filled_qty']):.2f} {coin}（约 {val:,.0f} USD）")
        # 判断依据（轻量呈现，不做术语堆砌）
        kw = {"新闻情绪": ("LLM",), "技术图形": ("量化",), "价格突破": ("突破",),
              "大资金动向": ("flow",)}
        src = {label: sum(1 for d in decisions
                          if any(t in str(d.get("reason") or "") for t in toks))
               for label, toks in kw.items()}
        hot = [f"{k}×{v}" for k, v in sorted(src.items(), key=lambda x: -x[1]) if v > 0][:2]
        if hot:
            L.append(f"主要依据：{'、'.join(hot)}")
        L.append("")

        # ---- 它现在的看法 ----
        conf = sum(float(d.get("confidence") or 0) for d in decisions) / len(decisions)
        sents = [float(d.get("llm_sentiment")) for d in decisions
                 if d.get("llm_sentiment") is not None]
        mood = _mood(sum(sents) / len(sents)) if sents else "还没有形成观点"
        L += ["🧠 **它现在的看法**", f"新闻氛围：{mood}", f"出手意愿：{_will(conf)}", ""]

    # ---- 系统状态 ----
    if klines:
        age = (datetime.now(timezone.utc) - _parse_ts(klines[-1]["ts"])).total_seconds() / 60
        if age > 5:
            L += ["⚠️ **系统状态**", f"行情数据已经 {age:.0f} 分钟没更新了，可能网络有问题"]
        else:
            line = f"一切正常，行情 {age:.0f} 分钟前刚更新"
            if decisions:
                line += f"；上一动作 {_parse_ts(decisions[0]['ts']):%H:%M}"
            L += ["✅ **系统状态**", line]
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="交易系统日报/快照（用户友好版）")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--symbol", default="BTC/USD")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--send", action="store_true", help="推送到飞书/企微（默认只预览）")
    p.add_argument("--snapshot", action="store_true",
                   help="实时快照模式：短窗口（默认1小时）+ 免打扰时段跳过")
    args = p.parse_args()

    if args.snapshot and args.hours == 24:
        args.hours = 1

    if not Path(args.db).exists():
        print(f"审计库不存在: {args.db}")
        return 1
    audit = AuditLog(args.db)
    title = "⏱ **美股趋势小助手 · 实时播报**" if args.snapshot else ""
    report = build_report(audit, args.symbol, args.hours, title=title)
    audit.close()

    if args.send:
        notifier = load_notifier()
        if not notifier.available:
            print("未配置 config/notify.yaml 的 webhook，跳过发送（定时器保持运行）")
            return 0
        if args.snapshot and notifier.should_mute():
            print("当前处于免打扰时段，跳过本轮快照")
            return 0
        ok = notifier.send_markdown(report)
        print("已推送" if ok else "推送失败（见服务端日志）")
        return 0 if ok else 1
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
