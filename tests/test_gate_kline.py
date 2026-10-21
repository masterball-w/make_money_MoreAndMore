"""Gate 现货 K 线字段顺序：收盘在下标 2，开盘在下标 5。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from info_layer.rest_kline import parse_gate_candle


# 官方样例形态：开盘价等于最低价，收盘贴近最高价。读反会把阳线看成阴线。
_ROW = ["1792138800", "27836.52", "82242.2", "82242.4", "82164.9", "82164.9", "0.338597", "true"]


def test_open_close_follow_official_order():
    candle = parse_gate_candle(_ROW)
    assert candle.close == 82242.2
    assert candle.open == 82164.9
    assert candle.high == 82242.4
    assert candle.low == 82164.9
    assert candle.volume == 0.338597
    assert candle.closed is True


def test_forming_bar_closed_flag():
    row = list(_ROW)
    row[7] = "false"
    assert parse_gate_candle(row).closed is False
