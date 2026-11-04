from __future__ import annotations

import io
import math
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.config import settings
from app.providers.errors import ProviderFailure
from app.providers.protocol import (
    ExtractionRequest,
    ExtractionResult,
    ProviderCapabilities,
    RecordInvocation,
    ReserveInvocation,
    validate_bounding_region,
    validate_confidence,
)
from app.schemas import ProviderInvoiceCandidate

AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION = "1.0.2"
AZURE_DOCUMENT_INTELLIGENCE_API_VERSION = "2024-11-30"
AZURE_DOCUMENT_INTELLIGENCE_MODEL = "prebuilt-invoice"
AZURE_DOCUMENT_INTELLIGENCE_STRATEGY = "prebuilt_invoice_v4"
MAX_DOCUMENT_PAGES = 10
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024

_INVOICE_FIELDS = {
    "InvoiceId": "invoice_number",
    "InvoiceDate": "invoice_date",
    "VendorTaxId": "supplier_tax_id",
    "VendorName": "supplier_name",
    "PurchaseOrder": "po_number",
    "CurrencyCode": "currency",
    "SubTotal": "subtotal",
    "TotalTax": "tax_total",
    "InvoiceTotal": "grand_total",
    "TotalDiscount": "discount_total",
    "FreightCharge": "shipping_total",
}
_LINE_FIELDS = {
    "ProductCode": "sku",
    "Description": "description",
    "Unit": "unit",
    "Quantity": "quantity",
    "UnitPrice": "unit_price",
    "Amount": "net_amount",
}


def _read(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _field(value: Any, sdk_name: str) -> Any:
    snake_name = []
    for index, character in enumerate(sdk_name):
        if index > 0 and character.isupper():
            snake_name.extend(("_", character.casefold()))
        else:
            snake_name.append(character.casefold())
    return _read(value, sdk_name, "".join(snake_name))


def _json_value(value: Any) -> Any:
    """Convert SDK region objects to JSON without deriving or filling coordinates."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return _json_value(as_dict())
    if hasattr(value, "x") and hasattr(value, "y"):
        return {"x": _json_value(value.x), "y": _json_value(value.y)}
    raise TypeError("Azure returned a non-JSON bounding region")


def _language_hint(locale: str) -> str:
    # The prebuilt invoice language table identifies Vietnamese as `vi`.
    return locale.split("-", 1)[0].casefold()


class AzureDocumentIntelligenceProvider:
    """Explicit live adapter for Azure's prebuilt invoice model."""

    name = "azure_document_intelligence"
    strategy = AZURE_DOCUMENT_INTELLIGENCE_STRATEGY

    def __init__(self, client: Any | None = None) -> None:
        self.client = client
        self._credential: Any | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text_input=True,
            image_input=True,
            max_pages=MAX_DOCUMENT_PAGES,
            max_bytes=MAX_DOCUMENT_BYTES,
            output_strategy=self.strategy,
            model_id=AZURE_DOCUMENT_INTELLIGENCE_MODEL,
        )

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        try:
            parsed = urlsplit(endpoint)
            _ = parsed.port
        except ValueError as exc:
            raise ProviderFailure(
                "PROVIDER_NOT_CONFIGURED",
                "DOCUMENT_INTELLIGENCE_ENDPOINT is invalid.",
                retryable=False,
                status_code=503,
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderFailure(
                "PROVIDER_NOT_CONFIGURED",
                "DOCUMENT_INTELLIGENCE_ENDPOINT must be an HTTPS resource endpoint.",
                retryable=False,
                status_code=503,
            )
        return endpoint.rstrip("/")

    def _make_client(self) -> Any:
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:
            raise ProviderFailure(
                "PROVIDER_DEPENDENCY_MISSING",
                "azure-ai-documentintelligence is not installed.",
                retryable=False,
                status_code=503,
            ) from exc

        endpoint = self._validate_endpoint(settings.document_intelligence_endpoint)
        if settings.document_intelligence_api_key:
            credential: Any = AzureKeyCredential(settings.document_intelligence_api_key)
        else:
            try:
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential()
            except ImportError as exc:
                raise ProviderFailure(
                    "PROVIDER_DEPENDENCY_MISSING",
                    "azure-identity is required for Entra authentication.",
                    retryable=False,
                    status_code=503,
                ) from exc
        self._credential = credential
        try:
            return DocumentIntelligenceClient(
                endpoint=endpoint,
                credential=credential,
                api_version=AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
                polling_interval=1,
                retry_total=0,
            )
        except Exception:
            self._close_credential()
            raise

    def _close_credential(self) -> None:
        credential, self._credential = self._credential, None
        close = getattr(credential, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _close_client(self, client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        self._close_credential()

    def extract_invoice(
        self,
        request: ExtractionRequest,
        *,
        reserve_invocation: ReserveInvocation | None = None,
        record_invocation: RecordInvocation | None = None,
    ) -> ExtractionResult:
        if not settings.allow_live_model_calls:
            raise ProviderFailure(
                "LIVE_CALLS_DISABLED",
                "Live provider calls are disabled. Set ALLOW_LIVE_MODEL_CALLS=true to enable them.",
                retryable=False,
                status_code=503,
            )
        if not settings.document_intelligence_endpoint:
            raise ProviderFailure(
                "PROVIDER_NOT_CONFIGURED",
                "DOCUMENT_INTELLIGENCE_ENDPOINT is required for Azure extraction.",
                retryable=False,
                status_code=503,
            )
        self._validate_request(request)

        client = self.client
        created_client = client is None
        if created_client:
            client = self._make_client()

        start_budget = time.monotonic()
        deadline = request.deadline_seconds_remaining
        remaining = float(settings.document_intelligence_read_timeout_seconds)
        if deadline is not None:
            remaining = min(remaining, deadline - (time.monotonic() - start_budget))
        if remaining < 1:
            if created_client:
                self._close_client(client)
            raise ProviderFailure(
                "JOB_DEADLINE_EXCEEDED",
                "Extraction exceeded its configured job deadline.",
                retryable=False,
            )
        connect_timeout = min(float(settings.document_intelligence_connect_timeout_seconds), remaining)
        call_id = ""
        try:
            if reserve_invocation:
                call_id = reserve_invocation(self.capabilities().model_id or "", self.strategy)
        except Exception:
            if created_client:
                self._close_client(client)
            raise
        call_started = time.monotonic()
        try:
            with io.BytesIO(request.document_bytes or b"") as body:
                poller = client.begin_analyze_document(
                    AZURE_DOCUMENT_INTELLIGENCE_MODEL,
                    body=body,
                    locale=_language_hint(request.locale),
                    connection_timeout=connect_timeout,
                    read_timeout=remaining,
                    logging_enable=False,
                )
                poll_result = poller.result(timeout=max(0.0, remaining - (time.monotonic() - call_started)))
                done = getattr(poller, "done", None)
                if callable(done) and not done():
                    raise TimeoutError("Azure analysis did not finish before the extraction deadline")
        except Exception as exc:
            failure = exc if isinstance(exc, ProviderFailure) else self._map_exception(exc)
            if record_invocation:
                record_invocation(
                    call_id,
                    latency_ms=int((time.monotonic() - call_started) * 1000),
                    usage=None,
                    failure_code=failure.code,
                )
            if failure is exc:
                raise
            raise failure from exc
        finally:
            if created_client:
                self._close_client(client)

        latency_ms = int((time.monotonic() - call_started) * 1000)
        try:
            candidate, evidence_texts, evidence_pages, evidence_bboxes, evidence_confidences = (
                self._parse_result(poll_result, request)
            )
        except ProviderFailure as failure:
            if record_invocation:
                record_invocation(
                    call_id,
                    latency_ms=latency_ms,
                    usage=None,
                    failure_code=failure.code,
                )
            raise

        if record_invocation:
            # Document Intelligence returns no token-usage count. Keep provider usage unknown.
            record_invocation(call_id, latency_ms=latency_ms, usage=None, failure_code=None)
        return ExtractionResult(
            provider=self.name,
            model_id=AZURE_DOCUMENT_INTELLIGENCE_MODEL,
            candidate=candidate,
            evidence_texts=evidence_texts,
            evidence_pages=evidence_pages,
            evidence_bboxes=evidence_bboxes,
            evidence_confidences=evidence_confidences,
            raw_output_ref=None,
            usage={"routing_reason": request.routing_reason},
            strategy=self.strategy,
        )

    @staticmethod
    def _validate_request(request: ExtractionRequest) -> None:
        if request.mime not in {"application/pdf", "image/png", "image/jpeg"}:
            raise ProviderFailure(
                "PROVIDER_DOCUMENT_UNSUPPORTED",
                "Azure extraction accepts PDF, PNG, or JPEG documents.",
                retryable=False,
                status_code=422,
            )
        if not isinstance(request.document_bytes, bytes) or not request.document_bytes:
            raise ProviderFailure(
                "PROVIDER_INVALID_INPUT",
                "The original document bytes are required by Azure extraction.",
                retryable=False,
                status_code=422,
            )
        if len(request.document_bytes) > MAX_DOCUMENT_BYTES:
            raise ProviderFailure(
                "INPUT_LIMIT_EXCEEDED",
                "Document exceeds the configured provider byte limit.",
                retryable=False,
                status_code=413,
            )
        page_numbers = [page.page_no for page in request.pages]
        if (
            not page_numbers
            or len(page_numbers) > MAX_DOCUMENT_PAGES
            or any(
                not isinstance(page_no, int) or isinstance(page_no, bool) or page_no < 1
                for page_no in page_numbers
            )
            or len(set(page_numbers)) != len(page_numbers)
        ):
            raise ProviderFailure(
                "INPUT_LIMIT_EXCEEDED",
                "Document page count is outside the configured provider limit.",
                retryable=False,
                status_code=422,
            )

    @classmethod
    def _parse_result(
        cls, result: Any, request: ExtractionRequest
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, int], dict[str, dict[str, Any]], dict[str, float]]:
        documents = _read(result, "documents")
        if not isinstance(documents, Sequence) or isinstance(documents, str | bytes):
            raise ProviderFailure(
                "PROVIDER_INVALID_OUTPUT",
                "Azure returned an invalid analysis result.",
                retryable=False,
            )
        if not documents:
            raise ProviderFailure(
                "PROVIDER_DOCUMENT_UNSUPPORTED",
                "Azure did not identify an invoice in this document.",
                retryable=False,
                status_code=422,
            )
        if len(documents) != 1:
            raise ProviderFailure(
                "PROVIDER_DOCUMENT_UNSUPPORTED",
                "This workflow accepts one invoice per document.",
                retryable=False,
                status_code=422,
            )
        fields = _read(documents[0], "fields")
        if not isinstance(fields, Mapping):
            raise ProviderFailure(
                "PROVIDER_INVALID_OUTPUT",
                "Azure returned invoice data in an invalid shape.",
                retryable=False,
            )

        candidate: dict[str, Any] = {"lines": []}
        evidence_texts: dict[str, str] = {}
        evidence_pages: dict[str, int] = {}
        evidence_bboxes: dict[str, dict[str, Any]] = {}
        evidence_confidences: dict[str, float] = {}
        valid_pages = {page.page_no for page in request.pages}

        def add_field(field: Any, path: str, destination: dict[str, Any], key: str) -> None:
            content = _read(field, "content")
            if content is None or (isinstance(content, str) and not content.strip()):
                return
            if not isinstance(content, str):
                raise ProviderFailure(
                    "PROVIDER_SCHEMA_INVALID",
                    "Azure returned a field value with an unsupported type.",
                    retryable=False,
                )
            destination[key] = content
            evidence_texts[path] = content[:2000]

            confidence = _read(field, "confidence")
            try:
                confidence = validate_confidence(confidence)
            except ValueError as exc:
                raise ProviderFailure(
                    "PROVIDER_INVALID_OUTPUT",
                    "Azure returned invalid confidence metadata.",
                    retryable=False,
                ) from exc
            if confidence is not None:
                evidence_confidences[path] = confidence

            regions = _read(field, "bounding_regions", "boundingRegions")
            if regions is None:
                return
            if not isinstance(regions, Sequence) or isinstance(regions, str | bytes):
                raise ProviderFailure(
                    "PROVIDER_INVALID_OUTPUT",
                    "Azure returned invalid bounding-region metadata.",
                    retryable=False,
                )
            if not regions:
                return
            try:
                region = validate_bounding_region(_json_value(regions[0]))
            except (TypeError, ValueError) as exc:
                raise ProviderFailure(
                    "PROVIDER_INVALID_OUTPUT",
                    "Azure returned invalid bounding-region metadata.",
                    retryable=False,
                ) from exc
            if region is None:
                return
            evidence_bboxes[path] = region
            page_no = _read(regions[0], "page_number", "pageNumber")
            if page_no is not None:
                if (
                    not isinstance(page_no, int)
                    or isinstance(page_no, bool)
                    or page_no not in valid_pages
                ):
                    raise ProviderFailure(
                        "PROVIDER_EVIDENCE_INVALID",
                        "Azure evidence references a page outside the uploaded document.",
                        retryable=False,
                    )
                evidence_pages[path] = page_no

        try:
            for azure_name, target_name in _INVOICE_FIELDS.items():
                field = _field(fields, azure_name)
                if field is not None:
                    add_field(field, target_name, candidate, target_name)

            items_field = _field(fields, "Items")
            if items_field is not None:
                items = _read(items_field, "value_array", "valueArray")
                if items is not None and (
                    not isinstance(items, Sequence) or isinstance(items, str | bytes)
                ):
                    raise ProviderFailure(
                        "PROVIDER_SCHEMA_INVALID",
                        "Azure returned invoice line items in an invalid shape.",
                        retryable=False,
                    )
                for item in items or []:
                    line_fields = _read(item, "value_object", "valueObject")
                    if not isinstance(line_fields, Mapping):
                        raise ProviderFailure(
                            "PROVIDER_SCHEMA_INVALID",
                            "Azure returned a malformed invoice line item.",
                            retryable=False,
                        )
                    line: dict[str, Any] = {}
                    line_no = len(candidate["lines"])
                    for azure_name, target_name in _LINE_FIELDS.items():
                        field = _field(line_fields, azure_name)
                        if field is not None:
                            add_field(field, f"lines[{line_no}].{target_name}", line, target_name)
                    if line:
                        candidate["lines"].append(line)

            validated = ProviderInvoiceCandidate.model_validate(candidate).model_dump(mode="json")
        except ValidationError as exc:
            raise ProviderFailure(
                "PROVIDER_SCHEMA_INVALID",
                "Azure output does not match the invoice extraction contract.",
                retryable=False,
            ) from exc
        return validated, evidence_texts, evidence_pages, evidence_bboxes, evidence_confidences

    @staticmethod
    def _map_exception(exc: Exception) -> ProviderFailure:
        status = _read(exc, "status_code")
        response = _read(exc, "response")
        if status is None and response is not None:
            status = _read(response, "status_code")
        headers = _read(response, "headers", default={}) or {}
        name = type(exc).__name__
        error = _read(exc, "error")
        error_code = _read(error, "code", default="")
        if not error_code and response is not None:
            body = getattr(response, "json", None)
            if callable(body):
                try:
                    error_code = _read(_read(body(), "error", default={}), "code", default="")
                except Exception:
                    error_code = ""
        code_text = str(error_code).casefold()

        if name in {
            "TimeoutError",
            "TimeoutException",
            "ServiceResponseTimeoutError",
            "ReadTimeoutError",
            "ConnectTimeoutError",
            "OperationNotDone",
        } or status in {408, 504}:
            return ProviderFailure(
                "PROVIDER_TIMEOUT",
                "Azure Document Intelligence exceeded the request deadline.",
                retryable=True,
            )
        auth_errors = {"ClientAuthenticationError", "CredentialUnavailableError", "AuthenticationError"}
        if name in auth_errors or status in {401, 403}:
            return ProviderFailure(
                "PROVIDER_AUTH_FAILED",
                "Azure Document Intelligence credentials were rejected or unavailable.",
                retryable=False,
            )
        if status == 429:
            retry_after = AzureDocumentIntelligenceProvider._retry_after(headers)
            return ProviderFailure(
                "PROVIDER_THROTTLED",
                "Azure Document Intelligence is throttling requests.",
                retryable=True,
                retry_after_seconds=retry_after,
            )
        if status == 413:
            return ProviderFailure(
                "INPUT_LIMIT_EXCEEDED", "Azure rejected the document size.", retryable=False, status_code=413
            )
        if status == 415 or any(
            marker in code_text for marker in ("invalidcontent", "unsupportedcontent", "invaliddocument")
        ):
            return ProviderFailure(
                "PROVIDER_DOCUMENT_UNSUPPORTED",
                "Azure does not support this document or its encoding.",
                retryable=False,
                status_code=422,
            )
        if name in {"ServiceRequestError", "ServiceResponseError", "ConnectionError"}:
            return ProviderFailure(
                "PROVIDER_NETWORK_ERROR", "Could not reach Azure Document Intelligence.", retryable=True
            )
        if isinstance(status, int) and status >= 500:
            return ProviderFailure(
                "PROVIDER_REMOTE_ERROR",
                "Azure Document Intelligence returned a server error.",
                retryable=True,
            )
        if isinstance(status, int) and 400 <= status < 500:
            return ProviderFailure(
                "PROVIDER_REQUEST_INVALID", "Azure Document Intelligence rejected the request.",
                retryable=False,
                status_code=422,
            )
        return ProviderFailure(
            "PROVIDER_FAILURE", "Azure Document Intelligence request failed.", retryable=True
        )

    @staticmethod
    def _retry_after(headers: Any) -> int | None:
        raw_ms = _read(headers, "retry-after-ms", "Retry-After-Ms")
        raw_seconds = _read(headers, "retry-after", "Retry-After")
        for raw, divisor in ((raw_ms, 1000.0), (raw_seconds, 1.0)):
            if raw is None:
                continue
            try:
                value = float(raw) / divisor
                if math.isfinite(value) and value >= 0:
                    return max(1, math.ceil(value))
            except (TypeError, ValueError):
                if divisor == 1.0:
                    try:
                        retry_at = parsedate_to_datetime(str(raw))
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=UTC)
                        return max(1, math.ceil((retry_at - datetime.now(UTC)).total_seconds()))
                    except (TypeError, ValueError, OverflowError):
                        pass
        return None
