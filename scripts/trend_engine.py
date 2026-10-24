"""日线趋势引擎 —— 美股虚拟盘主策略（2026-10-24 起）。

规则（与 BTC 版一致，市场切换到美股）：
  信号：SPY 日线收盘 > SMA200 → 持有半仓；跌破 → 清仓观望
  频率：每个交易日一次；美股收盘（ET 16:00）后定时器触发
  纪律：跌破均线无条件离场（崩盘保险）；零佣金（券商 ETF 免佣），滑点 2bp 模拟
  依据：SPY 25.8 年回测（2001-2026）分期——2023起 +16.2%/回撤10.2%、
        2025起 +11.1%/回撤10.2%（SMA200，全仓口径）

工程要点：
  - akshare 懒加载（本地无 akshare 也能跑单测）
  - 幂等以"最后一根已收盘日线的日期"为准（周末自动跳过）
  - 状态存 data/trend_state.json；决策/成交/日线写 data/audit.db
    （报告、机器人、H5 面板的数据源）
  - decide() 为纯函数（无 IO），单测直接覆盖买卖/基线/幂等分支
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

SLIPPAGE = 0.0002          # SPY 流动性极好，2bp 滑点已保守
FEE_RATE = 0.0             # 主流券商 ETF 零佣金

SYMBOL = "SPY"
ASSET_NAME = "标普500ETF(SPY)"


# ---------------------------------------------------------------- 数据层

def fetch_us_daily(symbol: str = SYMBOL, limit: int = 400):
    """拉取美股日线（akshare，懒加载）。返回 Candle 列表（旧→新，仅已收盘）。"""
    import akshare as ak

    from common.events import Candle, Market

    df = ak.stock_us_daily(symbol=symbol, adjust="qfq")
    rows = []
    for _, r in df.tail(limit).iterrows():
        d = r["date"]
        if hasattr(d, "to_pydatetime"):
            d = d.to_pydatetime()
        rows.append(Candle(
            ts=d.replace(tzinfo=timezone.utc),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            volume=float(r["volume"]), closed=True))
    return rows


# ---------------------------------------------------------------- 决策层（纯函数）

def compute_sma(closes: list[float], window: int) -> float | None:
    """最近 window 根收盘的简单均线；样本不足返回 None。"""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def decide(state: dict, price: float, sma: float, cfg: dict,
           candle_date: str) -> tuple[dict, dict | None]:
    """单步决策（纯函数）。

    state: {"cash","qty","avg_price","last_signal","last_candle","bootstrapped",...}
    返回 (new_state, trade)；trade 为 None 表示无操作，
    为 {"kind":"buy"/"sell","qty","px","fee","reason","pnl"} 时表示成交。
    """
    st = dict(state)
    exposure = float(cfg.get("exposure", 0.5))
    signal_on = price > sma

    if st.get("last_candle") == candle_date:
        return st, None                       # 同一根日线，幂等跳过

    if not st.get("bootstrapped"):
        st.update(last_signal=signal_on, last_candle=candle_date,
                  bootstrapped=True)
        return st, None                       # 首日只建基线

    trade = None
    if signal_on and st["qty"] <= 0:
        equity = st["cash"] + st["qty"] * price
        buy_usd = equity * exposure
        px = price * (1 + SLIPPAGE)
        qty = round(buy_usd / px, 4)
        trade = {"kind": "buy", "qty": qty, "px": px, "fee": 0.0,
                 "reason": f"日线趋势: 收盘{price:,.2f}上穿SMA{cfg.get('sma_window')}，"
                           f"进场（仓位{exposure:.0%}）"}
        st.update(cash=st["cash"] - buy_usd, qty=qty, avg_price=px)
    elif not signal_on and st["qty"] > 0:
        sell_qty = st["qty"]
        px = price * (1 - SLIPPAGE)
        proceeds = sell_qty * px
        pnl = (px / st["avg_price"] - 1) * 100 if st["avg_price"] else 0.0
        trade = {"kind": "sell", "qty": sell_qty, "px": px, "fee": 0.0,
                 "pnl": pnl,
                 "reason": f"日线趋势: 收盘{price:,.2f}跌破SMA{cfg.get('sma_window')}，"
                           f"清仓离场（本轮{pnl:+.1f}%，崩盘保险）"}
        st.update(cash=st["cash"] + proceeds, qty=0.0, avg_price=0.0)

    st["last_signal"] = signal_on
    st["last_candle"] = candle_date
    return st, trade


# ---------------------------------------------------------------- 编排层

def load_cfg() -> dict:
    try:
        import yaml

        data = yaml.safe_load((ROOT / "config" / "risk_limits.yaml")
                              .read_text(encoding="utf-8")) or {}
        return data.get("trend_engine") or {}
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    cfg = load_cfg()
    symbol = str(cfg.get("symbol", SYMBOL))
    window = int(cfg.get("sma_window", 200))
    exposure = float(cfg.get("exposure", 0.5))
    initial = float(cfg.get("initial_cash", 100_000.0))
    state_path = ROOT / "data" / "trend_state.json"

    candles = fetch_us_daily(symbol)
    if len(candles) < window + 2:
        print(f"日线不足（{len(candles)} < {window + 2}），跳过")
        return 0
    closes = [c.close for c in candles]
    sma = compute_sma(closes, window)
    assert sma is not None
    price = closes[-1]
    candle_date = candles[-1].ts.strftime("%Y-%m-%d")

    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "cash": initial, "qty": 0.0, "avg_price": 0.0,
        "last_signal": None, "last_candle": "", "bootstrapped": False}

    from common.events import Action, DecisionEvent, FillEvent, KlineEvent, Market, OrderStatus
    from common.notifier import load_notifier
    from common.version import load_version
    from decision_layer.audit import AuditLog

    state, trade = decide(state, price, sma,
                          {"sma_window": window, "exposure": exposure},
                          candle_date)

    audit = AuditLog(str(ROOT / "data" / "audit.db"))
    # 日线入审计库（H5 面板/报告的数据源；REPLACE 幂等）
    audit.backfill_klines(KlineEvent(
        symbol=symbol, market=Market.US_STOCK, interval="1d",
        current=None, history=candles[-200:]))
    traded = trade is not None
    if traded:
        now = datetime.now(timezone.utc)
        act = Action.BUY if trade["kind"] == "buy" else Action.SELL
        audit.log_decision(DecisionEvent(
            ts=now, action=act, symbol=symbol, market=Market.US_STOCK,
            quantity=trade["qty"], reference_price=trade["px"],
            confidence=0.8, reason=trade["reason"]))
        audit.log_fill(FillEvent(
            ts=now, symbol=symbol, market=Market.US_STOCK, side=act,
            filled_qty=trade["qty"], avg_price=trade["px"],
            commission=trade["fee"], status=OrderStatus.FILLED))

    eq = state["cash"] + state["qty"] * price
    state["high_water"] = max(state.get("high_water", initial), eq)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    audit.close()

    print(f"[趋势引擎|美股] {candle_date} {symbol} 收盘 {price:,.2f} | "
          f"SMA{window} {sma:,.2f} | 信号={'持有' if price > sma else '观望'} | "
          f"净值 {eq:,.0f} USD" + ("（本次有交易）" if traded else ""))
    notifier = load_notifier()
    if traded:
        notifier.send_markdown(
            f"📊 **美股趋势引擎交易**\n"
            f"{trade['reason']}\n"
            f"持仓 {state['qty']:.2f} {symbol} | 净值 {eq:,.0f} USD"
            f"（{(eq / initial - 1) * 100:+.2f}%）")
    elif state["last_candle"] == candle_date and not state.get("_notified_boot", False):
        state["_notified_boot"] = True
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        v = load_version()
        notifier.send_markdown(
            f"🇺🇸 **虚拟盘已切换美股：{ASSET_NAME}**\n"
            f"基线：收盘 {price:,.2f}，SMA{window} {sma:,.2f}，"
            f"初始信号={'持有' if price > sma else '观望'}。信号变化时开始交易。\n"
            f"版本 {v['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
