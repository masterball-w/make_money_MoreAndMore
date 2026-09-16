"""决策审计日志 —— SQLite 落盘，每笔决策可事后逐笔回放复盘。

记录内容：决策本身 + 输入快照（LLM 推理原文、量化信号明细、触发的新闻 ID）
+ 风控触发明细 + 成交回报。这是用户明确要求的风控项。
标准库 sqlite3 实现，无第三方依赖。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from common.events import DecisionEvent, FillEvent, KlineEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    action        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    market        TEXT NOT NULL,
    quantity      REAL NOT NULL,
    reference_price REAL NOT NULL,
    confidence    REAL NOT NULL,
    llm_sentiment REAL,
    quant_signal  REAL,
    reason        TEXT,
    llm_reasoning TEXT,
    news_ids      TEXT,
    risk_checks   TEXT,
    blocked_by_risk INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fills (
    order_id    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    decision_id TEXT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    filled_qty  REAL NOT NULL,
    avg_price   REAL NOT NULL,
    commission  REAL NOT NULL,
    status      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS klines (
    ts      TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  REAL NOT NULL,
    PRIMARY KEY (ts, symbol)
);
"""


class AuditLog:
    def __init__(self, db_path: Path | str = "data/audit.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def log_decision(self, d: DecisionEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d.id, _iso(d.ts), d.action.value, d.symbol, d.market.value,
                    d.quantity, d.reference_price, d.confidence,
                    d.llm_sentiment, d.quant_signal,
                    d.reason, d.llm_reasoning,
                    json.dumps(d.news_ids, ensure_ascii=False),
                    json.dumps(
                        [{"name": c.name, "passed": c.passed, "detail": c.detail}
                         for c in d.risk_checks],
                        ensure_ascii=False,
                    ),
                    int(d.blocked_by_risk),
                ),
            )
            self._conn.commit()

    def log_fill(self, f: FillEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f.order_id, _iso(f.ts), f.decision_id, f.symbol,
                    f.side.value, f.filled_qty, f.avg_price, f.commission,
                    f.status.value,
                ),
            )
            self._conn.commit()

    def log_kline(self, k: KlineEvent) -> None:
        """落盘 K 线流（供监控面板绘制实时 K 线图）。"""
        candle = k.current
        if candle is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?)",
                (_iso(candle.ts), k.symbol, candle.open, candle.high,
                 candle.low, candle.close, candle.volume),
            )
            self._conn.commit()

    # 查询（复盘/dashboard 用）

    def recent_klines(self, symbol: str, limit: int = 150) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM klines WHERE symbol=? ORDER BY ts DESC LIMIT ?",
            (symbol, limit))
        cols = [c[0] for c in cur.description]
        return list(reversed([dict(zip(cols, row)) for row in cur.fetchall()]))

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def recent_fills(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


def _iso(ts: datetime) -> str:
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()
