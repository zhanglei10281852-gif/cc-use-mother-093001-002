"""事件溯源：所有状态变更以追加事件落盘，重启后重放恢复。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Iterator

from .clock import fmt_dt

# 事件类型常量
STORM_OPENED = "StormOpened"
REPORT_RECEIVED = "ReportReceived"
ALERT_RAISED = "AlertRaised"
SNAPSHOT_FROZEN = "SnapshotFrozen"
PLAN_GENERATED = "PlanGenerated"
ACTIONS_SUPERSEDED = "ActionsSuperseded"
ACTION_APPROVED = "ActionApproved"
ACTION_REASSIGNED = "ActionReassigned"
ACTION_REVOKED = "ActionRevoked"
COMMAND_DISPATCHED = "CommandDispatched"
RECEIPT_ACCEPTED = "ReceiptAccepted"
COMMAND_ESCALATED = "CommandEscalated"


def make_event(seq: int, type_: str, at, payload: dict) -> dict:
    return {
        "seq": seq,
        "event_id": f"evt-{seq:08d}-{uuid.uuid4().hex[:8]}",
        "type": type_,
        "at": fmt_dt(at),
        "payload": payload,
    }


class EventStore:
    """JSONL 追加式事件日志。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._seq = 0
        for event in self.iter_events():
            self._seq = max(self._seq, int(event.get("seq", 0)))

    def __len__(self) -> int:
        return self._seq

    def append(self, type_: str, at, payload: dict) -> dict:
        self._seq += 1
        event = make_event(self._seq, type_, at, payload)
        line = json.dumps(event, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        return event

    def iter_events(self) -> Iterator[dict]:
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
