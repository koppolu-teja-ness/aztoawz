"""FastAPI surface package."""

from fastapi import FastAPI

from .approval import router as approval_router
from .errors import register_exception_handlers
from .migrations import router as migrations_router


def create_app() -> FastAPI:
    """Create API app with approval endpoints mounted."""

    app = FastAPI(title="Azure-to-AWS Migration Assistant API")
    register_exception_handlers(app)
    app.include_router(migrations_router)
    app.include_router(approval_router)
    return app


__all__ = ["create_app"]
