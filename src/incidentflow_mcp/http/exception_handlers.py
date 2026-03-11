"""
Global exception handlers.

Call register_exception_handlers(app) once during app construction to attach
structured JSON error responses for unhandled exceptions.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error"},
        )
