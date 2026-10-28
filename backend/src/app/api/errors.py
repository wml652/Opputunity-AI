from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.llm.provider import LLMProviderError
from app.domain.profile.documents import FileTooLargeError
from app.domain.shared.errors import (
    AuthenticationError,
    ConflictError,
    DecryptionError,
    DomainError,
    NotFoundError,
    ValidationError,
)

_STATUS: list[tuple[type[DomainError], int]] = [
    (LLMProviderError, 502),
    (FileTooLargeError, 413),
    (AuthenticationError, 401),
    (NotFoundError, 404),
    (ConflictError, 409),
    (ValidationError, 422),
    (DecryptionError, 500),
]


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_request: Request, exc: DomainError) -> JSONResponse:
        status = next((s for cls, s in _STATUS if isinstance(exc, cls)), 400)
        expose = status < 500 or isinstance(exc, LLMProviderError)  # upstream msg is redacted
        message = exc.message if expose else "internal error"
        return JSONResponse(error_body(exc.code, message), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values back (they may contain keys/passwords).
        fields = [".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()]
        return JSONResponse(
            error_body("validation_error", "invalid request: " + ", ".join(fields)),
            status_code=422,
        )
