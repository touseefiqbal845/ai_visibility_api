"""Application configuration, loaded from environment variables."""

from __future__ import annotations

import os


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///dev.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    JSON_SORT_KEYS = False

    # --- Anthropic ---
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    # Discovery and recommendation are low-frequency, high-value generation calls.
    # Scoring runs once per discovered query, so it gets the cheaper/faster model.
    MODEL_DISCOVERY = os.getenv("MODEL_DISCOVERY", "claude-sonnet-5")
    MODEL_SCORING = os.getenv("MODEL_SCORING", "claude-haiku-4-5")
    MODEL_RECOMMENDATION = os.getenv("MODEL_RECOMMENDATION", "claude-sonnet-5")
    LLM_TIMEOUT_SECONDS = _as_int(os.getenv("LLM_TIMEOUT_SECONDS"), 90)
    LLM_MAX_RETRIES = _as_int(os.getenv("LLM_MAX_RETRIES"), 1)
    # List price in USD per million tokens, for analytics only. Cache reads, cache
    # writes and batch discounts are not applied. A model missing from the table
    # uses the fallback pair, so an unknown id still produces a number you can SUM.
    LLM_MODEL_PRICING = {
        "claude-sonnet-5": (2.0, 10.0),
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
    }
    LLM_PRICE_INPUT_PER_MILLION = _as_float(os.getenv("LLM_PRICE_INPUT_PER_MILLION"), 2.0)
    LLM_PRICE_OUTPUT_PER_MILLION = _as_float(os.getenv("LLM_PRICE_OUTPUT_PER_MILLION"), 10.0)

    # --- DataForSEO ---
    DATAFORSEO_LOGIN = os.getenv("DATAFORSEO_LOGIN")
    DATAFORSEO_PASSWORD = os.getenv("DATAFORSEO_PASSWORD")
    DATAFORSEO_BASE_URL = os.getenv("DATAFORSEO_BASE_URL", "https://api.dataforseo.com")
    DATAFORSEO_LOCATION_CODE = _as_int(os.getenv("DATAFORSEO_LOCATION_CODE"), 2840)  # United States
    DATAFORSEO_LANGUAGE_CODE = os.getenv("DATAFORSEO_LANGUAGE_CODE", "en")
    DATAFORSEO_TIMEOUT_SECONDS = _as_int(os.getenv("DATAFORSEO_TIMEOUT_SECONDS"), 60)

    # --- Pipeline behaviour ---
    DISCOVERY_TARGET_QUERIES = _as_int(os.getenv("DISCOVERY_TARGET_QUERIES"), 15)
    RECOMMENDATION_COUNT = _as_int(os.getenv("RECOMMENDATION_COUNT"), 5)
    # How many of the highest-opportunity invisible queries get handed to Agent 3.
    RECOMMENDATION_INPUT_QUERIES = _as_int(os.getenv("RECOMMENDATION_INPUT_QUERIES"), 8)

    # --- Rate limiting ---
    RATELIMIT_ENABLED = _as_bool(os.getenv("RATELIMIT_ENABLED"), True)
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    PIPELINE_RATE_LIMIT = os.getenv("PIPELINE_RATE_LIMIT", "5 per hour")

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    RATELIMIT_ENABLED = False
    ANTHROPIC_API_KEY = "test-key"
    DATAFORSEO_LOGIN = "test-login"
    DATAFORSEO_PASSWORD = "test-password"
    LLM_MAX_RETRIES = 1
    # Flat rate so audit tests can assert the cost formula without a real model id.
    LLM_PRICE_INPUT_PER_MILLION = 1.0
    LLM_PRICE_OUTPUT_PER_MILLION = 2.0


_CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    key = (name or os.getenv("FLASK_ENV") or "development").lower()
    return _CONFIGS.get(key, DevelopmentConfig)
