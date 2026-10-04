"""时钟抽象：超时升级由注入时钟驱动，不依赖真实等待。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """真实时钟，不支持推进。"""

    def now(self) -> datetime:
        return utcnow()


class ManualClock:
    """测试用虚拟时钟，可任意推进。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def advance_to(self, moment: datetime) -> None:
        if moment < self._now:
            raise ValueError("虚拟时钟不能回拨")
        self._now = moment


class OffsetClock:
    """真实时间加持久化偏移的时钟。

    偏移量写入事件库 meta 表，CLI 跨进程 tick 与进程重启后
    虚拟时间仍然连续，值班人员无需真实等待即可触发超时升级。
    """

    META_KEY = "clock_offset_sec"

    def __init__(self, store) -> None:
        self._store = store

    def _offset(self) -> float:
        return float(self._store.get_meta(self.META_KEY, "0"))

    def now(self) -> datetime:
        return utcnow() + timedelta(seconds=self._offset())

    def advance(self, seconds: float) -> None:
        self._store.set_meta(self.META_KEY, repr(self._offset() + float(seconds)))

    def advance_to(self, moment: datetime) -> None:
        if moment < self.now():
            raise ValueError("虚拟时钟不能回拨")
        self._store.set_meta(self.META_KEY, repr((moment - utcnow()).total_seconds()))
