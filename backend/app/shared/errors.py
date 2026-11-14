from __future__ import annotations

from typing import Literal, TypedDict


DataErrorCode = Literal[
    "INVALID_REQUEST",
    "DATA_NOT_FOUND",
    "INVALID_SOURCE_PAYLOAD",
    "DATA_SOURCE_UNAVAILABLE",
    "DATA_SOURCE_TIMEOUT",
    "DATA_NOT_READY",
    "INTERNAL_ERROR",
]

ERROR_STATUSES: dict[DataErrorCode, int] = {
    "INVALID_REQUEST": 400,
    "DATA_NOT_FOUND": 404,
    "INVALID_SOURCE_PAYLOAD": 502,
    "DATA_SOURCE_UNAVAILABLE": 503,
    "DATA_SOURCE_TIMEOUT": 504,
    "DATA_NOT_READY": 503,
    "INTERNAL_ERROR": 500,
}


class ErrorDetail(TypedDict):
    field: str
    message: str


class DataServiceError(Exception):
    def __init__(
        self,
        code: DataErrorCode,
        message: str,
        details: list[ErrorDetail] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = ERROR_STATUSES[code]
        self.message = message
        self.details = details


def error_payload(error: DataServiceError, request_id: str) -> dict[str, object]:
    body: dict[str, object] = {
        "error": {
            "code": error.code,
            "message": error.message,
        },
        "meta": {"requestId": request_id},
    }
    if error.details:
        body["error"] = {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        }
    return body
