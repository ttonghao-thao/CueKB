"""Stable public errors; never expose provider payloads or credentials."""
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]]
    error_code: str
    trace_id: UUID
    request_id: str | None = None


class ServiceError(RuntimeError):
    def __init__(self, detail: str, error_code: str, status_code: int = 503) -> None:
        super().__init__(detail)
        self.detail = detail
        self.error_code = error_code
        self.status_code = status_code
