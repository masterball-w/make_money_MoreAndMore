"""经济事件日历 —— 重大事件（FOMC/非农/CPI/LPR）前的自动降仓窗口。

事件来源：config/economic_calendar.yaml（阶段2可接金十/ForexFactory 日历 API）。
风控规则：事件公布前 event_window_minutes 内，新开仓减半或 HOLD；
这是用户明确要求的风控项"重大事件前自动降仓"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("event_calendar")


@dataclass
class CalendarEvent:
    name: str
    at: datetime
    importance: str = "high"        # high / medium
    affected: list[str] | None = None


_DEFAULT_EVENTS: list[CalendarEvent] = [
    # 演示用静态日历；实装后由 API/配置驱动
]


class EconomicCalendar:
    def __init__(self, config_path: Path | None = None) -> None:
        self.events: list[CalendarEvent] = list(_DEFAULT_EVENTS)
        if config_path and config_path.exists():
            self._load_yaml(config_path)

    def _load_yaml(self, path: Path) -> None:
        try:
            import yaml
        except ImportError:
            logger.warning("pyyaml 未安装，使用内置事件")
            return
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for item in data.get("events", []):
            try:
                at = datetime.fromisoformat(str(item["at"]))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                self.events.append(
                    CalendarEvent(
                        name=str(item.get("name", "未命名")),
                        at=at,
                        importance=str(item.get("importance", "high")),
                        affected=list(item.get("affected", [])) or None,
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("日历条目解析失败 %s: %s", item, e)
        logger.info("经济日历加载: %d 个事件", len(self.events))

    def active_window(self, now: datetime | None = None, minutes_before: int = 60) -> CalendarEvent | None:
        """若当前处于某重要事件公布前 N 分钟窗口内，返回该事件，否则 None。"""
        now = now or datetime.now(timezone.utc)
        window_start = timedelta(minutes=minutes_before)
        for ev in self.events:
            if now <= ev.at and ev.at - now <= window_start:
                return ev
        return None

    def next_event(self, now: datetime | None = None) -> CalendarEvent | None:
        now = now or datetime.now(timezone.utc)
        upcoming = [e for e in self.events if e.at >= now]
        return min(upcoming, key=lambda e: e.at) if upcoming else None
