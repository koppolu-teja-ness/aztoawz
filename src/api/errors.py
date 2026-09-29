"""Centralized API exception handling with secret-safe responses."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def register_exception_handlers(app: FastAPI) -> None:
    """Register error handlers that avoid leaking raw payload values."""

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(  # type: ignore[unused-ignore]
        _: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors: list[dict[str, str]] = []
        for item in exc.errors():
            loc = ".".join(str(part) for part in item.get("loc", []))
            errors.append(
                {
                    "loc": loc or "unknown",
                    "type": str(item.get("type", "validation_error")),
                }
            )

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Invalid request payload",
                "errors": errors,
            },
        )

    @app.exception_handler(HTTPException)
    async def _handle_http_error(_: Request, exc: HTTPException) -> JSONResponse:  # type: ignore[unused-ignore]
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(_: Request, __: Exception) -> JSONResponse:  # type: ignore[unused-ignore]
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )


def secret_safe_detail(message: str, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an error detail object with optional non-sensitive metadata only."""

    payload: dict[str, Any] = {"message": message}
    if extras:
        payload["context"] = extras
    return payload
