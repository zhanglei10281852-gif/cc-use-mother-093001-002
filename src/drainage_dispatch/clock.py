"""可注入时钟：超时升级等时间逻辑不依赖真实等待。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO8601 字符串或透传 datetime，统一为 UTC  aware。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


class Clock:
    """时钟接口。"""

    def now(self) -> datetime:  # pragma: no cover - 接口定义
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


class VirtualClock(Clock):
    """测试与演示用虚拟时钟，可手动推进。"""

    def __init__(self, start: str | datetime = "2026-10-01T00:00:00+00:00"):
        self._now = parse_dt(start)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: str | datetime) -> None:
        self._now = parse_dt(moment)

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now
