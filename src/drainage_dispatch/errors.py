"""业务错误类型。"""


class DispatchError(Exception):
    """携带稳定错误码与 HTTP 状态码的调度业务错误。"""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
