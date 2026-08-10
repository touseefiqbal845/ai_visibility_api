"""Application factory."""

from __future__ import annotations

import uuid

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request

from app.api import BLUEPRINTS
from app.api.errors import register_error_handlers
from app.config import Config, get_config
from app.extensions import db, limiter, migrate
from app.utils.logging import configure_logging, set_correlation_id

load_dotenv()


def create_app(config_name: str | None = None, **overrides) -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))
    app.config.update(overrides)

    configure_logging(app.config.get("LOG_LEVEL", "INFO"))

    db.init_app(app)
    migrate.init_app(app, db)

    app.config.setdefault("RATELIMIT_STORAGE_URI", Config.RATELIMIT_STORAGE_URI)
    limiter.init_app(app)
    limiter.enabled = bool(app.config.get("RATELIMIT_ENABLED", True))

    # Imported for their side effect of registering with SQLAlchemy's metadata, which
    # Alembic autogenerate needs.
    from app import models  # noqa: F401

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)

    register_error_handlers(app)
    _register_request_hooks(app)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    return app


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def _assign_correlation_id():
        # Honour an inbound id so a request can be traced across services; mint one
        # otherwise. The pipeline swaps in the run uuid once a run starts.
        incoming = request.headers.get("X-Correlation-ID")
        correlation_id = incoming or str(uuid.uuid4())
        g.correlation_id = correlation_id
        set_correlation_id(correlation_id)

    @app.after_request
    def _echo_correlation_id(response):
        correlation_id = getattr(g, "correlation_id", None)
        if correlation_id:
            response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.teardown_request
    def _clear_correlation_id(exc=None):
        set_correlation_id(None)
