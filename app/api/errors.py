"""One error shape for every failure the API can produce.

    {"error": {"code": "not_found", "message": "...", "details": {...}}}

Clients should not have to branch on whether a 400 came from marshmallow, a 404 from
Flask's router, or a 500 from an unhandled exception.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify
from marshmallow import ValidationError
from werkzeug.exceptions import HTTPException

from app.utils.logging import get_correlation_id

logger = logging.getLogger(__name__)


class APIError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code or self.status_code
        self.code = code or self.code
        self.details = details or {}


class NotFoundError(APIError):
    status_code = 404
    code = "not_found"


class ValidationFailed(APIError):
    status_code = 422
    code = "validation_error"


class UpstreamError(APIError):
    """A dependency we do not control failed (model provider, DataForSEO)."""

    status_code = 502
    code = "upstream_error"


class ConflictError(APIError):
    status_code = 409
    code = "conflict"


def _payload(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    correlation_id = get_correlation_id()
    if correlation_id:
        body["error"]["correlation_id"] = correlation_id
    return body


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(APIError)
    def _api_error(exc: APIError):
        return jsonify(_payload(exc.code, exc.message, exc.details)), exc.status_code

    @app.errorhandler(ValidationError)
    def _marshmallow_error(exc: ValidationError):
        return (
            jsonify(
                _payload("validation_error", "Request body failed validation", exc.messages)
            ),
            422,
        )

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        code = (exc.name or "error").lower().replace(" ", "_")
        return jsonify(_payload(code, exc.description or exc.name)), exc.code or 500

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        # Log the detail, return a generic message: stack traces are not the client's
        # business, and the correlation id is enough to find this in the logs.
        logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        return jsonify(_payload("internal_error", "An unexpected error occurred")), 500
