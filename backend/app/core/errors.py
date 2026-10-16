"""统一业务异常，响应只暴露安全的业务说明，不返回原始参数或 SQL。"""

from typing import Any


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: Any = None):
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields
        super().__init__(message)
