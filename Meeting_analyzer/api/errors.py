"""Consistent error envelope and HTTP mapping.

Every error response has the shape::

    {"error": {"code": "...", "message": "..."}}

Request IDs are added by the logging middleware without content or secrets.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status as http_status


class ApiError(HTTPException):
    """HTTPException with a stable machine-readable code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code)
        self.code = code
        self.message = message


def not_found(code: str, message: str) -> ApiError:
    return ApiError(http_status.HTTP_404_NOT_FOUND, code, message)


def bad_request(code: str, message: str) -> ApiError:
    return ApiError(http_status.HTTP_400_BAD_REQUEST, code, message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(http_status.HTTP_409_CONFLICT, code, message)


def _envelope(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message),
            headers={"X-Request-Id": str(uuid.uuid4())},
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code", "http_error")
            message = detail.get("message", "Request failed.")
        else:
            code, message = "http_error", str(detail or "Request failed.")
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, message),
            headers={"X-Request-Id": str(uuid.uuid4())},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                "invalid_request",
                "The request payload failed validation: " + str(exc.errors()[0].get("msg", "")),
            ),
            headers={"X-Request-Id": str(uuid.uuid4())},
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        return JSONResponse(
            status_code=http_status.HTTP_409_CONFLICT,
            content=_envelope(
                "conflict", "The database rejected this change (integrity constraint)."
            ),
            headers={"X-Request-Id": str(uuid.uuid4())},
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError):
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_envelope("database_unavailable", "The database is unreachable."),
            headers={"X-Request-Id": str(uuid.uuid4())},
        )
