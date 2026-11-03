from app.domain.errors import DomainError


class ProviderFailure(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code, message, status_code or (503 if retryable else 422))
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
