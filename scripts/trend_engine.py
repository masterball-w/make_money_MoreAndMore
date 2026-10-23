"""日线趋势引擎 —— 主策略（2026-10-21 起，替代 1 分钟引擎）。

用户核心诉求：稳定盈利、等待上涨、不追涨杀跌。本引擎的形态：
  信号：BTC 日线收盘 > SMA150 → 持有半仓；跌破 → 清仓持币观望
  频率：每天只看一次（收盘后），一年换手约 10-20 次，费用可忽略
  纪律：跌破均线无条件离场（崩盘保险——"等涨上来"必须有一条硬底线）
  依据：13.6 年回测分期全正（2021起+33%/2023起+41%/2024起+26%/2025起+9.7%，
        同期死拿 2025 起 -6.5%）

执行细节：
  - systemd timer 每日 08:10 触发（幂等：同一天重复运行直接跳过）
  - 用最后一根【已收盘】日线算信号，按该收盘价模拟成交（taker 0.1%+滑点 0.05%）
  - 状态存 data/trend_state.json；首日只建基线不交易
  - 成交/决策写入 data/audit.db（日报、机器人、面板继续工作）
  - 仓位、均线窗口：config/risk_limits.yaml 的 trend_engine 段
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

from common.events import Action, DecisionEvent, FillEvent, Market, OrderStatus
from common.notifier import load_notifier
from common.version import load_version
from decision_layer.audit import AuditLog

DB = ROOT / "data" / "audit.db"
STATE = ROOT / "data" / "trend_state.json"
GATE = "https://api.gateio.ws/api/v4/spot/candlesticks"
SYMBOL = "BTC/USDT"


def load_cfg() -> dict:
    try:
        import yaml

        data = yaml.safe_load((ROOT / "config" / "risk_limits.yaml")
                              .read_text(encoding="utf-8")) or {}
        return data.get("trend_engine") or {}
    except Exception:  # noqa: BLE001
        return {}


def fetch_daily_closes(limit: int = 400) -> list[float]:
    r = httpx.get(GATE, params={"currency_pair": "BTC_USDT", "interval": "1d",
                                "limit": limit}, timeout=20)
    r.raise_for_status()
    rows = [c for c in r.json() if isinstance(c, list) and len(c) >= 8
            and str(c[7]).lower() == "true"]          # 只要已收盘日线
    rows.sort(key=lambda c: int(float(c[0])))          # 旧→新
    return [float(c[2]) for c in rows]                 # 下标2=收盘（Gate 字段序）


def main() -> int:
    cfg = load_cfg()
    window = int(cfg.get("sma_window", 150))
    exposure = float(cfg.get("exposure", 0.5))
    initial = float(cfg.get("initial_cash", 100_000.0))

    closes = fetch_daily_closes()
    if len(closes) < window + 2:
        print(f"日线不足（{len(closes)} < {window+2}），跳过")
        return 0
    sma = sum(closes[-window:]) / window
    price = closes[-1]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {
        "cash": initial, "qty": 0.0, "avg_price": 0.0,
        "last_signal": None, "last_run": "", "bootstrapped": False}

    if state.get("last_run") == today:
        print(f"{today} 已处理过，跳过（幂等）")
        return 0

    signal_on = price > sma
    equity = state["cash"] + state["qty"] * price
    v = load_version()
    print(f"[趋势引擎] {today} 收盘 {price:,.0f} | SMA{window} {sma:,.0f} | "
          f"信号={'持有' if signal_on else '观望'} | 净值 {equity:,.0f}")

    audit = AuditLog(str(DB))
    notifier = load_notifier()
    traded = False

    if not state.get("bootstrapped"):
        state.update(last_signal=signal_on, last_run=today, bootstrapped=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        audit.close()
        notifier.send_markdown(
            f"🔁 **策略切换：日线趋势引擎上线**\n"
            f"今日基线：收盘 {price:,.0f}，SMA{window} {sma:,.0f}，"
            f"初始信号={'持有(半仓)' if signal_on else '观望'}。首次信号变化时开始交易。\n"
            f"版本 {v['version']}")
        return 0

    prev = state.get("last_signal")
    if signal_on and state["qty"] <= 0 and prev is not None:
        # 观望 → 持有：用 exposure 比例净资产买入
        buy_usd = equity * exposure
        fill_px = price * 1.0005                       # 滑点
        fee = buy_usd * 0.001
        qty = round((buy_usd - fee) / fill_px, 6)
        state.update(cash=state["cash"] - buy_usd, qty=qty, avg_price=fill_px)
        _log(audit, "buy", qty, fill_px, fee,
             f"日线趋势: 收盘{price:,.0f}上穿SMA{window}，半仓进场（{exposure:.0%}）")
        traded = True
    elif not signal_on and state["qty"] > 0:
        sell_qty = state["qty"]
        fill_px = price * 0.9995
        proceeds = sell_qty * fill_px
        fee = proceeds * 0.001
        pnl = (fill_px / state["avg_price"] - 1) * 100 if state["avg_price"] else 0
        state.update(cash=state["cash"] + proceeds - fee, qty=0.0, avg_price=0.0)
        _log(audit, "sell", sell_qty, fill_px, fee,
             f"日线趋势: 收盘{price:,.0f}跌破SMA{window}，清仓离场（本轮{pnl:+.1f}%，"
             f"崩盘保险触发）")
        traded = True

    state["last_signal"] = signal_on
    state["last_run"] = today
    eq_new = state["cash"] + state["qty"] * price
    state["high_water"] = max(state.get("high_water", initial), eq_new)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    audit.close()

    if traded:
        last_fill_qty = state.get("qty", 0.0)
        notifier.send_markdown(
            f"📊 **趋势引擎信号变化**\n收盘 {price:,.0f} | SMA{window} {sma:,.0f}\n"
            f"当前状态：{'持有 ' + f'{last_fill_qty:.4f} BTC' if last_fill_qty > 0 else '空仓观望'}\n"
            f"账户净值 {eq_new:,.0f}（{(eq_new/initial-1)*100:+.2f}%）")
    return 0


def _log(audit: AuditLog, action: str, qty: float, px: float, fee: float,
         reason: str) -> None:
    now = datetime.now(timezone.utc)
    d = DecisionEvent(ts=now, action=Action.BUY if action == "buy" else Action.SELL,
                      symbol=SYMBOL, market=Market.CRYPTO, quantity=qty,
                      reference_price=px, confidence=0.8, reason=reason)
    audit.log_decision(d)
    audit.log_fill(FillEvent(ts=now, symbol=SYMBOL, market=Market.CRYPTO,
                             side=Action.BUY if action == "buy" else Action.SELL,
                             filled_qty=qty, avg_price=px, commission=fee,
                             status=OrderStatus.FILLED))


if __name__ == "__main__":
    sys.exit(main())
