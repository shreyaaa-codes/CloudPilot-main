"""Local database and optional read-only AWS topology-discovery configuration."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _enabled(name: str, default: bool) -> bool:
    """Read a conventional environment boolean without surprising fallbacks."""
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}

@dataclass(frozen=True)
class Settings:
    # SQLite makes V1 runnable locally. PostgreSQL works by setting DATABASE_URL.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./cloudpilot.db")
    aws_region: str = os.getenv("AWS_REGION", "eu-north-1")
    aws_profile: str | None = os.getenv("AWS_PROFILE") or None
    # Cost Explorer is an account-level billing API. The us-east-1 endpoint is
    # the documented portable default; callers can override it when required.
    cost_explorer_region: str = os.getenv("AWS_COST_EXPLORER_REGION", "us-east-1")
    app_environment: str = os.getenv("APP_ENV", "development").strip().lower()
    seed_demo_data: bool = _enabled("SEED_DEMO_DATA", os.getenv("APP_ENV", "development").strip().lower() != "production")
    cors_origins: tuple[str, ...] = tuple(origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip())

settings = Settings()
