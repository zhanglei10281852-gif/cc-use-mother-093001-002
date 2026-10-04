"""调度系统的领域错误类型。"""


class DispatchError(Exception):
    """所有调度领域错误的基类。"""

    status = 500


class ValidationError(DispatchError):
    """入参不合法。"""

    status = 400


class NotFoundError(DispatchError):
    """引用的对象不存在。"""

    status = 404


class ConflictError(DispatchError):
    """与当前状态冲突（幂等冲突、非法状态迁移等）。"""

    status = 409


class DeviceConflictError(ConflictError):
    """设备占用或联锁冲突，发令前被拦截。"""


class StateTransitionError(ConflictError):
    """动作/命令当前状态不允许该操作。"""
