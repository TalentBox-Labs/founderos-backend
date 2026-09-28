"""FastAPI middleware for logging and observability."""

from __future__ import annotations

import logging
import time
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from runner_api_routers.log_redaction import redact_query_string

logger = logging.getLogger(__name__)


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that logs HTTP requests and responses with structured fields.

    Logs:
    - Request method, path, status code
    - Request duration (ms)
    - Request/response size
    - Query parameters and path parameters (sensitive OAuth values redacted)
    - Errors with traceback
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log with structured fields."""
        start_time = time.time()
        request_size = int(request.headers.get("content-length", 0))

        # Extract relevant request info
        method = request.method
        path = request.url.path
        # Never log raw OAuth code/state/token query values.
        query_string = redact_query_string(request.url.query or "")

        try:
            response = await call_next(request)
            duration_ms = (time.time() - start_time) * 1000
            response_size = int(response.headers.get("content-length", 0))

            log_level = logging.INFO
            if response.status_code >= 500:
                log_level = logging.ERROR
            elif response.status_code >= 400:
                log_level = logging.WARNING

            logger.log(
                log_level,
                f"{method} {path} {response.status_code}",
                extra={
                    "method": method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "request_size": request_size,
                    "response_size": response_size,
                    "query_string": query_string,
                },
            )

            return response

        except ValueError as e:
            # Production auth (_verify_api_key) raises HTTPException(401).
            # Some callers/overrides still raise ValueError for bad credentials;
            # map only those explicit messages to 401. Do NOT substring-match
            # generic words like "key" (would misclassify validation errors).
            duration_ms = (time.time() - start_time) * 1000
            msg = str(e).strip()
            auth_messages = {
                "invalid key",
                "unauthorized",
                "invalid api key",
                "invalid credentials",
            }
            if msg.lower() in auth_messages:
                logger.warning(
                    f"{method} {path} auth/credential error",
                    extra={
                        "method": method,
                        "path": path,
                        "duration_ms": round(duration_ms, 2),
                        "error": msg,
                        "error_type": type(e).__name__,
                        "status_code": 401,
                    },
                )
                return JSONResponse(
                    status_code=401,
                    content={"detail": msg or "Unauthorized"},
                )
            # Non-auth ValueError: re-raise so ExceptionMiddleware / callers
            # handle it (avoid guessing 401/400 from free-form text).
            logger.error(
                f"{method} {path} raised exception",
                extra={
                    "method": method,
                    "path": path,
                    "duration_ms": round(duration_ms, 2),
                    "error": msg,
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            raise

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                f"{method} {path} raised exception",
                extra={
                    "method": method,
                    "path": path,
                    "duration_ms": round(duration_ms, 2),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            raise


class RequestBodyLoggingMiddleware(BaseHTTPMiddleware):
    """
    Optional middleware to log request bodies for debugging.

    Only logs on error to avoid logging sensitive data in production.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log body on error."""
        body_content = await request.body()
        async_gen = request.receive

        async def receive():
            return {"type": "http.request", "body": body_content}

        request._receive = receive

        try:
            response = await call_next(request)
            return response
        except Exception as e:
            # Log body only on error
            try:
                body_str = body_content.decode() if body_content else "(empty)"
                logger.error(
                    f"Request body on error: {request.method} {request.url.path}",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "body": body_str[:500],  # Truncate long bodies
                    },
                )
            except Exception:
                pass
            raise
