"""对账器 —— 定期比较券商侧持仓/余额与本地 Account 视图。

不一致时的处置：以券商侧为准修正本地状态，并告警（说明有漏单/手工干预/
分红除权等系统外事件）。阶段6实盘前实装定时对账任务。
"""
from __future__ import annotations

import logging

from common.models import Account

logger = logging.getLogger("reconciler")


class Reconciler:
    async def reconcile(self, account: Account, broker_positions: dict[str, float],
                        broker_cash: float) -> list[str]:
        """返回差异描述列表；空列表 = 完全一致。"""
        diffs: list[str] = []
        if abs(account.cash - broker_cash) > 1e-6:
            diffs.append(f"现金不一致: 本地{account.cash:.2f} vs 券商{broker_cash:.2f}")
            account.cash = broker_cash
        local = {s: p.qty for s, p in account.positions.items()}
        for symbol, qty in broker_positions.items():
            local_qty = local.pop(symbol, 0.0)
            if abs(local_qty - qty) > 1e-9:
                diffs.append(f"{symbol} 数量不一致: 本地{local_qty} vs 券商{qty}")
                if symbol in account.positions:
                    account.positions[symbol].qty = qty
        for symbol in local:
            if local[symbol] > 0:
                diffs.append(f"{symbol} 券商侧缺失（本地{local[symbol]}）")
                account.positions[symbol].qty = 0.0
        if diffs:
            logger.warning("[对账] 发现 %d 处差异: %s", len(diffs), "; ".join(diffs))
        return diffs
