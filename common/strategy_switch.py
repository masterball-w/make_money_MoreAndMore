"""策略切换与共享组合状态 —— 三策略（定投 / 投机 / 混合并行）的唯一状态入口。

设计约定：
  - data/strategy_state.json   当前策略 + 切换历史（飞书指令 / H5 按钮共用）
  - data/portfolio_state.json  共享组合（现金/持仓/高水位 + 各策略私有字段）
    首次缺失时从旧 trend_state.json 一次性迁移（现金与 SPY 底仓原样继承）
  - 切换策略不清仓：持仓保留移交。定投继承 SPY 底仓；
    投机冻结 SPY（spec 引擎永不交易 SPY），只用剩余现金。
  - hybrid（默认）：dca 管 SPY 底仓 + spec 并行跑七巨头事件投机，两引擎共享组合，
    用 acquire_portfolio_lock() 串行化读写，避免现金竞态/重复下单。
所有函数带 path 参数，单测用 tmp_path 隔离，不碰真实数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STRATEGIES = {"dca": "稳健长期定投", "spec": "短期投机", "hybrid": "混合并行（定投+事件投机）"}
DEFAULT_STRATEGY = "hybrid"
INITIAL_CASH = 100_000.0
_HISTORY_CAP = 20


def acquire_portfolio_lock(blocking: bool = True):
    """跨引擎共享组合锁（Linux flock；Windows 无 fcntl 降级为无锁）。

    hybrid 下 dca（每日）与 spec（20分钟/5分钟）并行读写同一组合，须串行化。
    返回文件句柄（进程退出自动释放）；非阻塞且被占用时返回 None。
    """
    try:
        import fcntl
    except ImportError:  # noqa: BLE001 —— Windows 本地开发/单测不锁
        return True
    p = ROOT / "data" / "portfolio.lock"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "w")
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, flags)
        return fh
    except OSError:
        return None


def strategy_label(name: str) -> str:
    return STRATEGIES.get(name, name)


def _strategy_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else ROOT / "data" / "strategy_state.json"


def _portfolio_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else ROOT / "data" / "portfolio_state.json"


def _trend_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else ROOT / "data" / "trend_state.json"


# ---------------------------------------------------------------- 策略状态

def load_strategy_state(path: Path | str | None = None) -> dict:
    """读当前策略状态；文件缺失/损坏一律回退默认 dca（系统降级原则）。"""
    st = {"strategy": DEFAULT_STRATEGY, "source": "default",
          "updated_at": "", "history": []}
    p = _strategy_path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                name = str(data.get("strategy") or "")
                if name in STRATEGIES:
                    st["strategy"] = name
                st["source"] = str(data.get("source") or "")
                st["updated_at"] = str(data.get("updated_at") or "")
                hist = data.get("history")
                if isinstance(hist, list):
                    st["history"] = hist[-_HISTORY_CAP:]
        except Exception:  # noqa: BLE001 —— 状态文件异常不阻断交易
            pass
    return st


def load_strategy(path: Path | str | None = None) -> str:
    return load_strategy_state(path)["strategy"]


def set_strategy(name: str, source: str = "manual",
                 path: Path | str | None = None) -> dict:
    """切换策略（校验 + 落盘 + 历史 cap 20）。非法名称抛 ValueError。"""
    if name not in STRATEGIES:
        raise ValueError(f"未知策略: {name}（可选: {' / '.join(STRATEGIES)}）")
    st = load_strategy_state(path)
    now = datetime.now(timezone.utc).isoformat()
    st["strategy"] = name
    st["source"] = source
    st["updated_at"] = now
    st["history"] = (st["history"] + [
        {"strategy": name, "source": source, "ts": now}])[-_HISTORY_CAP:]
    p = _strategy_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return st


# ---------------------------------------------------------------- 共享组合

def default_portfolio(initial_cash: float = INITIAL_CASH) -> dict:
    return {"cash": float(initial_cash), "initial_cash": float(initial_cash),
            "high_water": float(initial_cash), "positions": {},
            "dca": {"last_candle": "", "last_dca_date": "", "last_dip_date": "",
                    "last_dip_drop": 0.0,
                    "last_price": 0.0, "last_sma": 0.0, "sma_window": 200},
            "spec": {"seen": [], "watch": {}, "last_trade": {}, "last_run": ""}}


def _migrate_from_trend(trend_path: Path) -> dict | None:
    """旧趋势引擎状态 → 共享组合（现金 + SPY 底仓 + 高水位原样继承）。"""
    try:
        st = json.loads(trend_path.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            return None
    except Exception:  # noqa: BLE001
        return None
    pf = default_portfolio()
    pf["cash"] = float(st.get("cash") or INITIAL_CASH)
    qty = float(st.get("qty") or 0)
    avg = float(st.get("avg_price") or 0)
    last_price = float(st.get("last_price") or 0)
    if qty > 0:
        pf["positions"]["SPY"] = {"qty": qty, "avg_price": avg,
                                  "trail_high": max(avg, last_price),
                                  "tp_stage": 0}
    eq = pf["cash"] + qty * last_price
    pf["high_water"] = float(st.get("high_water") or max(eq, INITIAL_CASH))
    return pf


def load_portfolio(path: Path | str | None = None,
                   trend_path: Path | str | None = None) -> dict:
    """读共享组合；缺失时从 trend_state.json 一次性迁移并落盘。"""
    p = _portfolio_path(path)
    if p.exists():
        try:
            pf = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(pf, dict):
                base = default_portfolio(float(pf.get("initial_cash") or INITIAL_CASH))
                for k, v in base.items():
                    pf.setdefault(k, v)
                for sub in ("dca", "spec"):
                    cur = pf.get(sub)
                    if not isinstance(cur, dict):
                        cur = {}
                    for k, v in base[sub].items():
                        cur.setdefault(k, v)
                    pf[sub] = cur
                if not isinstance(pf.get("positions"), dict):
                    pf["positions"] = {}
                return pf
        except Exception:  # noqa: BLE001
            pass
    tp = _trend_path(trend_path)
    pf = _migrate_from_trend(tp) if tp.exists() else None
    if pf is None:
        pf = default_portfolio()
    save_portfolio(pf, path)
    return pf


def save_portfolio(pf: dict, path: Path | str | None = None) -> None:
    p = _portfolio_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pf, ensure_ascii=False, indent=1), encoding="utf-8")
