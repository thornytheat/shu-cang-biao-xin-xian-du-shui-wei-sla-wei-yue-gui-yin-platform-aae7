"""统一业务异常。"""
from __future__ import annotations

from typing import Any

from src.constants.error_codes import ERROR_CODES


class AppError(Exception):
    def __init__(self, code: str, status_code: int = 400, detail: Any = None) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        self.message = ERROR_CODES.get(code, code)
        super().__init__(self.message)


class ProbeCollectionError(Exception):
    """单表采集失败（超时 / 取数异常）。

    不中断整批：捕获后把对应明细标记 failed、批次挂起，等待断点续跑。
    """

    def __init__(self, table_code: str, reason: str) -> None:
        self.table_code = table_code
        self.reason = reason
        super().__init__(f"probe collection failed for {table_code}: {reason}")

