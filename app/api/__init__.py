from app.api.profiles import bp as profiles_bp
from app.api.queries import bp as queries_bp

BLUEPRINTS = (profiles_bp, queries_bp)

__all__ = ["BLUEPRINTS", "profiles_bp", "queries_bp"]
