"""跟随账本 —— 一笔大额流入就是一位带单人。

Gate 公开成交流没有机构身份，所以带单人用资金流信号 id 表示。
止损后拉黑这一位，在出现下一笔合格的流入之前不再开仓。
止盈或信号离场只结束本次跟随，不拉黑。
"""
from __future__ import annotations


class FollowBook:
    def __init__(self) -> None:
        self.leader: dict[str, str] = {}
        self.blocked: dict[str, set[str]] = {}
        self.waiting: set[str] = set()

    def can_enter(self, symbol: str, leader_id: str | None, kind: str,
                  strength: float, min_strength: float) -> bool:
        """是否允许这笔开仓。拉黑的带单人永远不能再跟。"""
        if leader_id and leader_id in self.blocked.get(symbol, set()):
            return False
        if symbol not in self.waiting:
            return True
        return kind == "flow_in" and bool(leader_id) and strength >= min_strength

    def begin(self, symbol: str, leader_id: str) -> None:
        self.leader[symbol] = leader_id
        self.waiting.discard(symbol)

    def finish(self, symbol: str, *, failed: bool) -> str | None:
        """结束跟随。failed=True 表示止损，拉黑并等待下一位。"""
        leader = self.leader.pop(symbol, None)
        if failed and leader:
            self.blocked.setdefault(symbol, set()).add(leader)
            self.waiting.add(symbol)
        return leader
