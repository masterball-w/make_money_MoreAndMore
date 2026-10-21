"""15 分钟趋势过滤：样本不足时不开仓，方向由快慢 EMA 决定。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.events import Candle
from decision_layer.strategy import fifteen_minute_trend


def _series(n: int, price_at) -> list[Candle]:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        px = price_at(i)
        out.append(Candle(ts=t0 + timedelta(minutes=i), open=px, high=px, low=px, close=px))
    return out


def test_short_history_is_flat():
    assert fifteen_minute_trend(_series(30, lambda i: 100 + i)) == 0.0


def test_rising_series_is_up():
    assert fifteen_minute_trend(_series(21 * 15, lambda i: 100 + i * 0.2)) == 1.0


def test_falling_series_is_down():
    assert fifteen_minute_trend(_series(21 * 15, lambda i: 200 - i * 0.2)) == -1.0
