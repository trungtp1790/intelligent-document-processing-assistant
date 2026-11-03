from __future__ import annotations

import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def main() -> None:
    if ENV_FILE.exists():
        raise SystemExit(".env already exists; leaving it unchanged")
    values = {
        "APP_ENV": "development",
        "DATABASE_URL": "postgresql+psycopg://idp:local-only-change-me@postgres:5432/idp",
        "REDIS_URL": "redis://redis:6379/0",
        "STORAGE_BACKEND": "local",
        "DOCUMENT_STORAGE_PATH": "/var/lib/idp/documents",
        "EXTRACTION_PROVIDER": "fixture",
        "ALLOW_LIVE_MODEL_CALLS": "false",
        "MAX_DOCUMENT_PAGES": "10",
        "MAX_FILE_MIB": "15",
        "JWT_SECRET": secrets.token_urlsafe(48),
        "DEMO_PASSWORD": secrets.token_urlsafe(18),
        "DEMO_OPERATOR_EMAIL": "operator@demo.local",
        "DEMO_REVIEWER_EMAIL": "reviewer@demo.local",
        "FRONTEND_ORIGIN": "http://localhost:5173",
    }
    ENV_FILE.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )
    print(
        "Created local .env with random JWT secret and demo password. Keep it private."
    )


if __name__ == "__main__":
    main()
