from app.config import settings
from app.domain.errors import DomainError
from app.providers.bedrock import BedrockProvider
from app.providers.fixture import FixtureProvider
from app.providers.foundry import FoundryProvider
from app.providers.protocol import ExtractionProvider


def configured_provider() -> ExtractionProvider:
    if settings.extraction_provider == "fixture":
        return FixtureProvider()
    if settings.extraction_provider == "bedrock":
        return BedrockProvider()
    if settings.extraction_provider == "foundry":
        return FoundryProvider()
    raise DomainError("PROVIDER_NOT_CONFIGURED", "EXTRACTION_PROVIDER is not supported.", 503)
