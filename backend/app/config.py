from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://idp:local-only-change-me@localhost:5432/idp"
    redis_url: str = "redis://localhost:6379/0"
    storage_backend: str = "local"
    document_storage_path: str = "./var/documents"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_server_side_encryption: str = "AES256"
    s3_kms_key_id: str = ""
    extraction_provider: str = "fixture"
    allow_live_model_calls: bool = False
    copilot_provider: str = "disabled"
    copilot_timeout_seconds: int = 45
    copilot_max_output_tokens: int = 768
    aws_region: str = ""
    bedrock_model_id: str = ""
    bedrock_connect_timeout_seconds: int = 10
    bedrock_read_timeout_seconds: int = 180
    bedrock_max_output_tokens: int = 4096
    foundry_model_endpoint: str = ""
    foundry_deployment: str = ""
    foundry_api_key: str = ""
    foundry_connect_timeout_seconds: int = 10
    foundry_read_timeout_seconds: int = 180
    foundry_max_output_tokens: int = 4096
    foundry_temperature: float = 0.0
    extraction_job_deadline_seconds: int = 600
    extraction_prompt_version: str = "invoice-v1"
    workflow_engine: str = "langgraph"
    max_document_pages: int = 10
    max_file_mib: int = 15
    jwt_secret: str = ""
    jwt_issuer: str = "idp-assistant"
    jwt_audience: str = "idp-assistant-ui"
    access_token_minutes: int = 30
    demo_password: str = ""
    demo_operator_email: str = "operator@demo.local"
    demo_reviewer_email: str = "reviewer@demo.local"
    frontend_origin: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def _is_placeholder(value: str) -> bool:
    normalized = "".join(char for char in value.casefold() if char.isalnum())
    return normalized in {
        "changeme",
        "developmentonly",
        "example",
        "localonlychangeme",
        "password",
        "postgres",
        "replaceme",
        "secret",
        "yourpassword",
    } or normalized.startswith(("change", "replace", "your"))


def _is_example_host(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    example_domains = ("example.com", "example.net", "example.org")
    return any(host == domain or host.endswith(f".{domain}") for domain in example_domains)


def _is_production_foundry_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/").casefold() == "/openai/v1"
        and not _is_example_host(parsed.hostname)
    )


def validate_runtime_settings(config: Settings | None = None) -> None:
    active = config or settings
    if active.app_env not in {"development", "test", "staging", "production"}:
        raise RuntimeError("APP_ENV must be development, test, staging, or production")
    if active.storage_backend not in {"local", "s3"}:
        raise RuntimeError("STORAGE_BACKEND must be 'local' or 's3'")
    if active.s3_server_side_encryption not in {"AES256", "aws:kms"}:
        raise RuntimeError("S3_SERVER_SIDE_ENCRYPTION must be AES256 or aws:kms")
    if active.s3_server_side_encryption == "aws:kms" and not active.s3_kms_key_id:
        raise RuntimeError("S3_KMS_KEY_ID is required when using aws:kms encryption")
    if (
        min(
            active.bedrock_connect_timeout_seconds,
            active.bedrock_read_timeout_seconds,
            active.bedrock_max_output_tokens,
            active.foundry_connect_timeout_seconds,
            active.foundry_read_timeout_seconds,
            active.foundry_max_output_tokens,
            active.extraction_job_deadline_seconds,
            active.copilot_timeout_seconds,
            active.copilot_max_output_tokens,
        )
        < 1
    ):
        raise RuntimeError("Provider timeouts, output token limits, and extraction deadline must be positive")
    if active.storage_backend == "s3" and (not active.s3_bucket or not active.aws_region):
        raise RuntimeError("S3_BUCKET and AWS_REGION are required for S3 document storage")
    if active.app_env == "production":
        jwt_secret = active.jwt_secret.strip()
        if len(jwt_secret) < 32 or len(set(jwt_secret)) < 16 or _is_placeholder(jwt_secret):
            raise RuntimeError("JWT_SECRET must be at least 32 characters in production")
        if active.demo_password:
            raise RuntimeError("DEMO_PASSWORD must be empty in production; do not seed demo accounts")
        if active.storage_backend != "s3":
            raise RuntimeError("Production requires private S3-compatible document storage")
        if _is_placeholder(active.s3_bucket) or _is_placeholder(active.aws_region):
            raise RuntimeError("S3_BUCKET and AWS_REGION must contain provisioned values in production")
        if active.s3_server_side_encryption == "aws:kms" and _is_placeholder(active.s3_kms_key_id):
            raise RuntimeError("S3_KMS_KEY_ID must contain a provisioned KMS key in production")
        if active.extraction_provider not in {"bedrock", "foundry"}:
            raise RuntimeError("Production requires a configured live extraction provider")
        if not active.allow_live_model_calls:
            raise RuntimeError("Production requires explicit ALLOW_LIVE_MODEL_CALLS=true")
        frontend = urlsplit(active.frontend_origin)
        if frontend.scheme != "https" or not frontend.hostname:
            raise RuntimeError("FRONTEND_ORIGIN must be an HTTPS origin in production")
        if _is_example_host(frontend.hostname):
            raise RuntimeError("FRONTEND_ORIGIN must use the provisioned production hostname")
        has_url_extras = (
            frontend.username
            or frontend.password
            or frontend.path not in {"", "/"}
            or frontend.query
            or frontend.fragment
        )
        if has_url_extras:
            raise RuntimeError("FRONTEND_ORIGIN must contain only an HTTPS scheme, host, and optional port")
        if frontend.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("FRONTEND_ORIGIN cannot use localhost in production")
        database = urlsplit(active.database_url)
        if database.scheme != "postgresql+psycopg" or not database.hostname or not database.password:
            raise RuntimeError("Production DATABASE_URL must use PostgreSQL with psycopg")
        database_password = unquote(database.password)
        if len(database_password) < 16 or _is_placeholder(database_password):
            raise RuntimeError("Production DATABASE_URL must use a strong, non-placeholder password")
        local_database = database.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
        if local_database or _is_example_host(database.hostname):
            raise RuntimeError("Production DATABASE_URL must use a provisioned external database host")
        if parse_qs(database.query).get("sslmode", [""])[0] not in {"require", "verify-ca", "verify-full"}:
            raise RuntimeError("Production DATABASE_URL must set sslmode=require, verify-ca, or verify-full")
        redis = urlsplit(active.redis_url)
        if redis.scheme != "rediss" or not redis.hostname or not redis.password:
            raise RuntimeError("Production REDIS_URL must use TLS Redis with authentication")
        redis_password = unquote(redis.password)
        if len(redis_password) < 16 or _is_placeholder(redis_password):
            raise RuntimeError("Production REDIS_URL must use a strong, non-placeholder password")
        if redis.hostname.casefold() in {"localhost", "127.0.0.1", "::1"} or _is_example_host(redis.hostname):
            raise RuntimeError("Production REDIS_URL must use a provisioned external Redis host")
        if active.s3_endpoint_url:
            s3_endpoint = urlsplit(active.s3_endpoint_url)
            credentials_present = s3_endpoint.username or s3_endpoint.password
            if s3_endpoint.scheme != "https" or not s3_endpoint.hostname or credentials_present:
                raise RuntimeError("S3_ENDPOINT_URL must be a credential-free HTTPS endpoint in production")
    if active.extraction_provider == "bedrock":
        if not active.allow_live_model_calls:
            raise RuntimeError("Set ALLOW_LIVE_MODEL_CALLS=true before enabling Bedrock extraction")
        if not active.aws_region or not active.bedrock_model_id:
            raise RuntimeError("AWS_REGION and BEDROCK_MODEL_ID are required for Bedrock extraction")
        if active.app_env == "production" and (
            _is_placeholder(active.aws_region) or _is_placeholder(active.bedrock_model_id)
        ):
            raise RuntimeError(
                "AWS_REGION and BEDROCK_MODEL_ID must contain provisioned values in production"
            )
    if active.extraction_provider == "foundry":
        if not active.allow_live_model_calls:
            raise RuntimeError("Set ALLOW_LIVE_MODEL_CALLS=true before enabling Foundry extraction")
        if not active.foundry_model_endpoint or not active.foundry_deployment:
            raise RuntimeError(
                "FOUNDRY_MODEL_ENDPOINT and FOUNDRY_DEPLOYMENT are required for Foundry extraction"
            )
        if not 0 <= active.foundry_temperature <= 2:
            raise RuntimeError("FOUNDRY_TEMPERATURE must be between 0 and 2")
    if active.copilot_provider not in {"disabled", "foundry"}:
        raise RuntimeError("COPILOT_PROVIDER must be 'disabled' or 'foundry'")
    if active.copilot_provider == "foundry":
        if not active.allow_live_model_calls:
            raise RuntimeError("Set ALLOW_LIVE_MODEL_CALLS=true before enabling the Foundry copilot")
        if not active.foundry_model_endpoint or not active.foundry_deployment:
            raise RuntimeError("FOUNDRY_MODEL_ENDPOINT and FOUNDRY_DEPLOYMENT are required for the copilot")
    if active.app_env == "production" and (
        active.extraction_provider == "foundry" or active.copilot_provider == "foundry"
    ):
        if not _is_production_foundry_endpoint(active.foundry_model_endpoint) or _is_placeholder(
            active.foundry_deployment
        ):
            raise RuntimeError(
                "Production Foundry FOUNDRY_MODEL_ENDPOINT must use HTTPS /openai/v1 "
                "and a provisioned deployment"
            )
    if active.workflow_engine not in {"legacy", "langgraph"}:
        raise RuntimeError("WORKFLOW_ENGINE must be 'legacy' or 'langgraph'")
