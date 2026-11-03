from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from app.config import settings
from app.domain.errors import DomainError


@dataclass(frozen=True)
class ValidatedDocument:
    mime: str
    page_count: int
    page_hashes: list[str]


def validate_document(data: bytes) -> ValidatedDocument:
    max_bytes = settings.max_file_mib * 1024 * 1024
    if not data:
        raise DomainError("EMPTY_FILE", "Tệp rỗng không thể tải lên.", 422)
    if len(data) > max_bytes:
        raise DomainError("FILE_TOO_LARGE", f"Tệp vượt giới hạn {settings.max_file_mib} MiB.", 413)

    if data.startswith(b"%PDF-"):
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise DomainError("PDF_PASSWORD_PROTECTED", "PDF có mật khẩu không được hỗ trợ.", 422)
            page_count = len(reader.pages)
            if page_count < 1:
                raise DomainError("INVALID_DOCUMENT", "PDF không có trang.", 422)
            if page_count > settings.max_document_pages:
                raise DomainError("TOO_MANY_PAGES", f"Tối đa {settings.max_document_pages} trang.", 422)
            page_hashes = []
            whole_hash = hashlib.sha256(data).hexdigest()
            for index, page in enumerate(reader.pages, start=1):
                text = " ".join((page.extract_text() or "").split())
                page_hashes.append(hashlib.sha256((text or f"{whole_hash}:{index}").encode()).hexdigest())
            return ValidatedDocument("application/pdf", page_count, page_hashes)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError("INVALID_DOCUMENT", "PDF hỏng hoặc không đọc được.", 422) from exc

    try:
        image = Image.open(io.BytesIO(data))
        image.verify()
        image = Image.open(io.BytesIO(data))
        if image.format == "PNG":
            mime = "image/png"
        elif image.format in {"JPEG", "JPG"}:
            mime = "image/jpeg"
        else:
            raise DomainError("UNSUPPORTED_MEDIA_TYPE", "Chỉ hỗ trợ PDF, PNG và JPEG.", 415)
        width, height = image.size
        if width * height > 25_000_000:
            raise DomainError("IMAGE_TOO_LARGE", "Ảnh vượt giới hạn 25 megapixel.", 422)
        return ValidatedDocument(mime, 1, [hashlib.sha256(data).hexdigest()])
    except DomainError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DomainError("UNSUPPORTED_MEDIA_TYPE", "Chỉ hỗ trợ PDF, PNG và JPEG hợp lệ.", 415) from exc
