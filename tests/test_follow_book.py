"""跟随账本：止损拉黑当前带单人，下一笔合格流入才能再进。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision_layer.follow_book import FollowBook


def test_stop_blocks_same_leader_and_accepts_the_next():
    book = FollowBook()
    assert book.can_enter("BTC/USDT", "flow-a", "flow_in", 0.8, 0.45)
    book.begin("BTC/USDT", "flow-a")
    assert book.finish("BTC/USDT", failed=True) == "flow-a"
    assert book.can_enter("BTC/USDT", "flow-a", "flow_in", 0.9, 0.45) is False
    assert book.can_enter("BTC/USDT", "break-1", "donch_up", 1.0, 0.45) is False
    assert book.can_enter("BTC/USDT", "flow-b", "flow_in", 0.3, 0.45) is False
    assert book.can_enter("BTC/USDT", "flow-b", "flow_in", 0.7, 0.45) is True
    book.begin("BTC/USDT", "flow-b")
    assert "BTC/USDT" not in book.waiting
    assert book.leader["BTC/USDT"] == "flow-b"


def test_take_profit_does_not_blacklist():
    book = FollowBook()
    book.begin("BTC/USDT", "flow-a")
    assert book.finish("BTC/USDT", failed=False) == "flow-a"
    assert book.can_enter("BTC/USDT", "flow-a", "flow_in", 0.8, 0.45) is True
    assert "BTC/USDT" not in book.waiting
