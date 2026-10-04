"""排涝联合调度领域包。"""
from .clock import SystemClock, VirtualClock
from .contracts import CommandAction, DrainageDevice, StormSnapshot
from .errors import (
    ConflictError,
    DeviceConflictError,
    DispatchError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from .registry import Registry
from .service import DispatchService

__all__ = [
    "CommandAction",
    "DrainageDevice",
    "StormSnapshot",
    "DispatchService",
    "Registry",
    "SystemClock",
    "VirtualClock",
    "DispatchError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "DeviceConflictError",
    "StateTransitionError",
]
