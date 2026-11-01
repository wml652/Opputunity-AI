from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.container import ApiContainer
from app.api.errors import install_error_handlers
from app.api.middleware.security import CsrfMiddleware, RequestContextMiddleware
from app.api.v1 import (
    applications,
    auth,
    discovery,
    events,
    hitl,
    llm,
    sessions,
    system,
    training,
    vault,
)

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_api(container: ApiContainer, lifespan: Lifespan | None = None) -> FastAPI:
    docs = container.config.expose_docs
    app = FastAPI(
        title="Opportunity Platform API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )
    app.state.container = container

    v1 = APIRouter(prefix="/api/v1")
    for module in (
        auth,
        llm,
        events,
        system,
        vault,
        discovery,
        applications,
        sessions,
        training,
        hitl,
    ):
        v1.include_router(module.router)
    app.include_router(v1)

    install_error_handlers(app)
    # Order: outermost last. Request context wraps everything; CSRF runs before routing.
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=container.config.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-csrf-token", "authorization", "last-event-id"],
    )
    app.add_middleware(RequestContextMiddleware, hsts=container.config.cookie_secure)
    return app
