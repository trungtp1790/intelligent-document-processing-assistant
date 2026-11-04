from __future__ import annotations

import pytest

from app.config import Settings, validate_runtime_settings
from app.seed import seed


def _production_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "database_url": "postgresql+psycopg://idp:strong-database-password@db.northwind-internal.net:5432/idp?sslmode=verify-full",
        "redis_url": "rediss://redis-user:strong-redis-password@redis.northwind-internal.net:6379/0",
        "storage_backend": "s3",
        "s3_bucket": "private-invoice-documents",
        "aws_region": "us-east-1",
        "extraction_provider": "foundry",
        "allow_live_model_calls": True,
        "foundry_model_endpoint": "https://northwind.services.ai.azure.com/openai/v1/",
        "foundry_deployment": "invoice-extraction",
        "jwt_secret": "N6#zT8qW1!vR4pX2@cG9mK7sD3$uF5hJ",
        "demo_password": "",
        "frontend_origin": "https://invoices.northwind-internal.net",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_valid_production_settings_pass_runtime_validation() -> None:
    validate_runtime_settings(_production_settings())


def test_production_can_select_azure_document_intelligence_explicitly() -> None:
    validate_runtime_settings(
        _production_settings(
            extraction_provider="azure_document_intelligence",
            document_intelligence_endpoint="https://northwind.cognitiveservices.azure.com/",
        )
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"app_env": "prod"}, "APP_ENV"),
        ({"jwt_secret": "short"}, "JWT_SECRET"),
        ({"demo_password": "seeded-demo-password"}, "DEMO_PASSWORD"),
        ({"storage_backend": "local"}, "private S3-compatible"),
        ({"extraction_provider": "fixture", "allow_live_model_calls": False}, "live extraction provider"),
        ({"allow_live_model_calls": False}, "ALLOW_LIVE_MODEL_CALLS"),
        ({"frontend_origin": "http://invoices.example.com"}, "HTTPS origin"),
        ({"frontend_origin": "https://invoices.example.com"}, "provisioned"),
        (
            {
                "database_url": "postgresql+psycopg://idp:strong-database-password@localhost:5432/"
                "idp?sslmode=verify-full"
            },
            "external database host",
        ),
        (
            {"database_url": "postgresql+psycopg://idp:strong-database-password@db.northwind.net:5432/idp"},
            "sslmode",
        ),
        (
            {
                "database_url": "postgresql+psycopg://idp:replace-me@db.northwind.net:5432/"
                "idp?sslmode=verify-full"
            },
            "strong",
        ),
        ({"redis_url": "redis://redis-user:redis-pass@redis.internal:6379/0"}, "TLS Redis"),
        ({"redis_url": "rediss://redis.internal:6379/0"}, "TLS Redis"),
        ({"redis_url": "rediss://redis-user:replace-me@redis.internal:6379/0"}, "strong"),
        ({"s3_endpoint_url": "http://object-store.internal"}, "S3_ENDPOINT_URL"),
        ({"s3_bucket": "replace-me"}, "provisioned values"),
        ({"foundry_model_endpoint": "http://model.internal/openai/v1/"}, "FOUNDRY_MODEL_ENDPOINT"),
        ({"s3_server_side_encryption": "aws:kms", "s3_kms_key_id": ""}, "S3_KMS_KEY_ID"),
        ({"jwt_secret": "a" * 64}, "JWT_SECRET"),
    ],
)
def test_production_validation_rejects_unsafe_or_incomplete_settings(overrides, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_runtime_settings(_production_settings(**overrides))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://northwind.services.ai.azure.com/openai/v1/",
        "https://user:secret@northwind.services.ai.azure.com/openai/v1/",
        "https://northwind.services.ai.azure.com/openai/v1/?api-key=secret",
        "https://northwind.services.ai.azure.com/other/",
        "https://northwind.example.com/openai/v1/",
    ],
)
def test_production_copilot_rejects_unsafe_foundry_endpoint(endpoint: str) -> None:
    validate_settings = _production_settings(
        extraction_provider="bedrock",
        aws_region="us-east-1",
        bedrock_model_id="anthropic.claude-sonnet-4-20250514-v1:0",
        copilot_provider="foundry",
        foundry_model_endpoint=endpoint,
    )
    with pytest.raises(RuntimeError, match="Production Foundry"):
        validate_runtime_settings(validate_settings)


def test_development_fixture_configuration_remains_supported() -> None:
    validate_runtime_settings(Settings(_env_file=None, app_env="development"))


def test_demo_seed_is_disabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.seed.settings.app_env", "production")

    with pytest.raises(SystemExit, match="disabled in production"):
        seed()
