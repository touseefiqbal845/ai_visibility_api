"""Extension singletons, instantiated here so the app factory can bind them late."""

from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
