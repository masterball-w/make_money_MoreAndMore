"""稳健长期定投引擎 —— 双策略之一（2026-11-17 起）。

规则（标的 SPY，每个交易日评估一次，以最新已收盘日线为准）：
  门禁：收盘 > SMA200 才允许周定投；回调低吸豁免门禁（深回调仍低吸，越跌越买）
  周定投：每周固定日（默认周一）买入固定金额（默认 $2000）
  逢低买入：距近 20 日最高价回撤 5%/10%/15% 分级加仓 1/2/3 倍周定投额，
            两次低吸间隔 ≥ 5 个自然日，但回调更深时豁免冷却（自动识别回调）
  止盈：浮盈 +10% 卖出 1/3 落袋；剩余仓位跟踪止盈（距最高点回撤 8% 清仓）
  纪律：先卖后买；同一根日线幂等；零佣金 + 2bp 滑点

工程要点：
  - 共享组合 data/portfolio_state.json（common.strategy_switch 唯一入口）
  - decide() 纯函数（深拷贝，无 IO），单测直接覆盖全分支
  - 决策/成交/日线写 data/audit.db（报告、机器人、H5 面板的数据源）
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from trend_engine import SLIPPAGE, compute_sma, fetch_us_daily  # noqa: E402

SYMBOL = "SPY"


def load_cfg() -> dict:
    try:
        import yaml

        data = yaml.safe_load((ROOT / "config" / "risk_limits.yaml")
                              .read_text(encoding="utf-8")) or {}
        return data.get("dca_engine") or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 决策层（纯函数）

def decide(pf: dict, price: float, sma: float | None, closes: list[float],
           cfg: dict, candle_date: str) -> tuple[dict, list[dict]]:
    """单步决策（纯函数，深拷贝入参）。

    pf: 共享组合（common.strategy_switch.default_portfolio 结构）
    返回 (new_pf, trades)；trades 元素
    {"kind","symbol","qty","px","fee","reason","pnl"?}，空列表=无操作。
    """
    st = copy.deepcopy(pf)
    symbol = str(cfg.get("symbol", SYMBOL))
    dca = st.setdefault("dca", {})
    pos_all = st.setdefault("positions", {})

    # 展示字段先刷新（重跑也更新报告/面板数据），再做幂等检查
    dca["last_price"] = price
    dca["last_sma"] = sma if sma is not None else 0.0
    dca["sma_window"] = cfg.get("sma_window", 200)
    if dca.get("last_candle") == candle_date:
        return st, []

    trades: list[dict] = []
    tp_trigger = float(cfg.get("tp_trigger", 0.10))
    tp_fraction = float(cfg.get("tp_fraction", 0.3333))
    trail_pct = float(cfg.get("trail_pct", 0.08))

    # ---- 先卖：分批止盈 + 跟踪止盈 ----
    pos = pos_all.get(symbol)
    if pos and pos.get("qty", 0) > 0:
        avg = float(pos.get("avg_price") or 0)
        pnl = price / avg - 1 if avg > 0 else 0.0
        if pos.get("tp_stage", 0) == 0 and pnl >= tp_trigger:
            sell_qty = round(pos["qty"] * tp_fraction, 4)
            if sell_qty > 0:
                px = price * (1 - SLIPPAGE)
                trades.append({
                    "kind": "sell", "symbol": symbol, "qty": sell_qty, "px": px,
                    "fee": 0.0, "pnl": (px / avg - 1) * 100 if avg > 0 else 0.0,
                    "reason": f"定投止盈: 浮盈{pnl * 100:+.1f}%达到+{tp_trigger * 100:.0f}%，"
                              f"卖出{tp_fraction:.0%}落袋，剩余转跟踪止盈"})
                st["cash"] += sell_qty * px
                pos["qty"] = round(pos["qty"] - sell_qty, 8)
                pos["tp_stage"] = 1
                pos["trail_high"] = price
                if pos["qty"] <= 1e-9:
                    pos_all.pop(symbol, None)
                    pos = None
        if pos and pos.get("tp_stage", 0) >= 1:
            pos["trail_high"] = max(float(pos.get("trail_high") or price), price)
            if price <= pos["trail_high"] * (1 - trail_pct):
                sell_qty = pos["qty"]
                px = price * (1 - SLIPPAGE)
                trades.append({
                    "kind": "sell", "symbol": symbol, "qty": sell_qty, "px": px,
                    "fee": 0.0, "pnl": (px / avg - 1) * 100 if avg > 0 else 0.0,
                    "reason": f"跟踪止盈: 收盘{price:,.2f}距最高点{pos['trail_high']:,.2f}"
                              f"回撤超{trail_pct:.0%}，清仓锁定利润"})
                st["cash"] += sell_qty * px
                pos_all.pop(symbol, None)

    # ---- 后买：周定投受门禁约束；回调低吸豁免门禁（深回调恰恰是低吸时机）----
    gate = sma is not None and price > sma
    weekly_amount = float(cfg.get("weekly_amount", 2000))
    weekly_day = int(cfg.get("weekly_day", 0))
    dip_lookback = int(cfg.get("dip_lookback", 20))
    dip_tiers = cfg.get("dip_tiers") or []
    cooldown = int(cfg.get("dip_cooldown_days", 5))
    day = datetime.strptime(candle_date, "%Y-%m-%d")

    buy_usd, reason, mark, drop = 0.0, "", "", 0.0
    if gate and day.weekday() == weekly_day and dca.get("last_dca_date") != candle_date:
        buy_usd = weekly_amount
        mark = "dca"
        reason = (f"周定投: 每周定投日买入 {weekly_amount:,.0f} USD "
                  f"(收盘{price:,.2f} > SMA{dca['sma_window']}，门禁开启)")
    else:
        # 回调低吸独立于门禁：跌破 SMA200 的深回调不再被"趋势保护"误杀
        window = closes[-dip_lookback:]
        if window and max(window) > 0:
            drop = 1 - price / max(window)
            tier = None
            for t in sorted(dip_tiers, key=lambda x: float(x.get("drop", 0))):
                if drop >= float(t.get("drop", 0)):
                    tier = t
            if tier is not None:
                last_dip = dca.get("last_dip_date") or ""
                last_drop = float(dca.get("last_dip_drop") or 0.0)
                elapsed = True
                if last_dip:
                    elapsed = (day - datetime.strptime(last_dip, "%Y-%m-%d")).days >= cooldown
                # 越跌越买：回调比上次低吸更深 → 豁免冷却，允许分级加仓
                deeper = drop > last_drop + 1e-6
                if elapsed or deeper:
                    mult = float(tier.get("mult", 1))
                    buy_usd = weekly_amount * mult
                    mark = "dip"
                    reason = (f"逢低买入: 距近{dip_lookback}日高点回调{drop * 100:.1f}%，"
                              f"触发{mult:.0f}倍定投加仓 {buy_usd:,.0f} USD"
                              + ("" if gate else "（跌破SMA200深回调豁免门禁）"))
    if buy_usd > 0:
        buy_usd = min(buy_usd, st["cash"])
        px = price * (1 + SLIPPAGE)
        qty = round(buy_usd / px, 4)
        if qty * px > st["cash"]:   # 四舍五入可能超出几分钱现金 → 向下取整兜底
            qty = int(st["cash"] / px * 10000) / 10000
        if qty > 0:
            old = pos_all.get(symbol)
            if old and old.get("qty", 0) > 0:
                new_qty = old["qty"] + qty
                old["avg_price"] = (old["avg_price"] * old["qty"] + px * qty) / new_qty
                old["qty"] = new_qty
                old["trail_high"] = max(float(old.get("trail_high") or price), price)
            else:
                pos_all[symbol] = {"qty": qty, "avg_price": px,
                                   "trail_high": price, "tp_stage": 0}
            st["cash"] -= qty * px
            trades.append({"kind": "buy", "symbol": symbol, "qty": qty,
                           "px": px, "fee": 0.0, "reason": reason})
            if mark == "dca":
                dca["last_dca_date"] = candle_date
            elif mark == "dip":
                dca["last_dip_date"] = candle_date
                dca["last_dip_drop"] = drop

    dca["last_candle"] = candle_date
    return st, trades


# ---------------------------------------------------------------- 编排层

def main() -> int:
    from common.strategy_switch import (acquire_portfolio_lock, load_portfolio,
                                        load_strategy, save_portfolio)

    if load_strategy() not in ("dca", "hybrid"):
        print("[定投引擎] 当前策略未启用定投，跳过")
        return 0
    lock = acquire_portfolio_lock(blocking=True)   # 与 spec 串行化共享组合，防现金竞态
    if lock is None:
        print("[定投引擎] 组合锁获取失败，跳过")
        return 0
    cfg = load_cfg()
    symbol = str(cfg.get("symbol", SYMBOL))
    window = int(cfg.get("sma_window", 200))

    candles = fetch_us_daily(symbol)
    if len(candles) < window + 2:
        print(f"日线不足（{len(candles)} < {window + 2}），跳过")
        return 0
    closes = [c.close for c in candles]
    sma = compute_sma(closes, window)
    price = closes[-1]
    candle_date = candles[-1].ts.strftime("%Y-%m-%d")

    pf = load_portfolio()
    pf, trades = decide(pf, price, sma, closes, cfg, candle_date)

    from common.events import Action, DecisionEvent, FillEvent, KlineEvent, Market, OrderStatus
    from common.notifier import load_notifier
    from decision_layer.audit import AuditLog

    audit = AuditLog(str(ROOT / "data" / "audit.db"))
    audit.backfill_klines(KlineEvent(
        symbol=symbol, market=Market.US_STOCK, interval="1d",
        current=None, history=candles[-200:]))
    now = datetime.now(timezone.utc)
    for t in trades:
        act = Action.BUY if t["kind"] == "buy" else Action.SELL
        dec = DecisionEvent(
            ts=now, action=act, symbol=t["symbol"], market=Market.US_STOCK,
            quantity=t["qty"], reference_price=t["px"],
            confidence=0.8, reason=t["reason"])
        audit.log_decision(dec)
        # order_id 是 fills 表主键：必须唯一，否则 INSERT OR REPLACE 覆盖上一笔成交
        audit.log_fill(FillEvent(
            ts=now, symbol=t["symbol"], market=Market.US_STOCK, side=act,
            filled_qty=t["qty"], avg_price=t["px"],
            commission=t["fee"], status=OrderStatus.FILLED,
            decision_id=dec.id, order_id=dec.id))

    eq = pf["cash"]
    for sym, p in pf["positions"].items():
        px = price if sym == symbol else float(p.get("avg_price") or 0)
        eq += p.get("qty", 0) * px
    pf["high_water"] = max(float(pf.get("high_water") or 0), eq)
    save_portfolio(pf)
    audit.close()

    gate = sma is not None and price > sma
    print(f"[定投引擎] {candle_date} {symbol} 收盘 {price:,.2f} | "
          f"SMA{window} {sma:,.2f} | 买入门禁={'开' if gate else '暂停'} | "
          f"净值 {eq:,.0f} USD" + ("（本次有交易）" if trades else ""))
    if trades:
        lines = [f"📊 **稳健定投引擎交易**（{candle_date} 收盘）"]
        lines += [t["reason"] for t in trades]
        spy = pf["positions"].get(symbol)
        held = f"持仓 {spy['qty']:.2f} {symbol}" if spy else f"{symbol} 空仓"
        lines.append(f"{held} | 净值 {eq:,.0f} USD")
        load_notifier().send_markdown("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
