from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "evals" / "datasets" / "synthetic_invoice_corpus_v1"
DOCUMENTS_DIR = CORPUS_DIR / "documents"
LABELS_DIR = CORPUS_DIR / "labels"
sys.path.insert(0, str(ROOT / "backend"))

from app.domain.reconciliation import reconcile  # noqa: E402
from app.schemas import InvoiceData  # noqa: E402

FAMILIES = (
    ("dev", "dev_plain_english", "en", "plain"),
    ("dev", "dev_table_bilingual", "mixed", "table"),
    ("dev", "dev_vietnamese_tax", "vi", "tax"),
    ("validation", "validation_compact_vietnamese", "vi", "compact"),
    ("validation", "validation_two_column_english", "en", "two_column"),
    ("test", "test_vat_invoice_vietnamese", "vi", "tax"),
    ("test", "test_remittance_english", "en", "remittance"),
    ("test", "test_multpage_mixed", "mixed", "multipage"),
    ("test", "test_scanned_ledger_vietnamese", "vi", "ledger"),
)

VI_LABELS = {
    "invoice": "HOA DON",
    "invoice_number": "So hoa don",
    "series": "Ky hieu",
    "date": "Ngay",
    "tax_id": "Ma so thue",
    "supplier": "Nha cung cap",
    "po": "So don mua",
    "currency": "Tien te",
    "subtotal": "Cong tien hang",
    "tax": "Tien thue",
    "total": "Tong thanh toan",
    "discount": "Chiet khau",
    "shipping": "Van chuyen",
    "sku": "Ma hang",
    "description": "Dien giai",
    "quantity": "So luong",
    "unit": "Don vi",
    "unit_price": "Don gia",
    "net": "Thanh tien",
    "none": "Khong co",
    "continued": "Tiep theo",
}
EN_LABELS = {
    "invoice": "INVOICE",
    "invoice_number": "Invoice number",
    "series": "Series",
    "date": "Invoice date",
    "tax_id": "Supplier tax ID",
    "supplier": "Supplier",
    "po": "Purchase order",
    "currency": "Currency",
    "subtotal": "Subtotal",
    "tax": "Tax total",
    "total": "Grand total",
    "discount": "Discount",
    "shipping": "Shipping",
    "sku": "SKU",
    "description": "Description",
    "quantity": "Quantity",
    "unit": "Unit",
    "unit_price": "Unit price",
    "net": "Line total",
    "none": "None",
    "continued": "Continued",
}
FONT_ALIASES = ("SyntheticEvalSans", "SyntheticEvalBold")


def _font_path(explicit: str | None) -> Path:
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ["IDP_EVAL_FONT"]) if os.environ.get("IDP_EVAL_FONT") else None,
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise SystemExit(
        "A Unicode TrueType font is required. Pass --font or set IDP_EVAL_FONT "
        "(for example Arial or DejaVu Sans)."
    )


def _language(family_language: str, ordinal: int) -> str:
    if family_language == "mixed":
        return "vi-VN" if ordinal % 2 else "en-US"
    return "vi-VN" if family_language == "vi" else "en-US"


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _display_decimal(value: str, language: str) -> str:
    formatted = f"{Decimal(value):,.2f}"
    if language.startswith("vi"):
        return formatted.replace(",", "#").replace(".", ",").replace("#", ".")
    return formatted


def _label_values(family: str, ordinal: int, language: str) -> tuple[dict[str, Any], dict[str, Any]]:
    family_code = "".join(part[0] for part in family.split("_") if part)[:5].upper()
    supplier_tax_id = f"SYN-{family_code}-{ordinal:03d}"
    currency = "USD" if ordinal == 2 else "VND"
    line_count = 1 + ordinal % 3
    lines: list[dict[str, str | None]] = []
    po_lines: list[dict[str, str]] = []
    for line_no in range(1, line_count + 1):
        quantity = Decimal(1 + ((ordinal + line_no) % 7))
        unit_price = Decimal((ordinal + line_no) * 12750) + Decimal("0.25")
        net_amount = (quantity * unit_price).quantize(Decimal("0.01"))
        sku = None if ordinal == 4 and line_no == 1 else f"SKU-{family_code}-{ordinal:02d}-{line_no}"
        description = f"Synthetic item {family_code} {ordinal:02d}-{line_no}"
        line = {
            "sku": sku,
            "description": description,
            "unit": "piece",
            "quantity": _fmt_decimal(quantity),
            "unit_price": _fmt_decimal(unit_price),
            "net_amount": _fmt_decimal(net_amount),
        }
        lines.append(line)
        po_lines.append(
            {
                "sku": sku or f"PO-SKU-{family_code}-{ordinal:02d}-{line_no}",
                "description": description,
                "unit": "piece",
                "quantity": _fmt_decimal(quantity + Decimal(1) if ordinal == 5 else quantity),
                "unit_price": _fmt_decimal(unit_price),
            }
        )

    subtotal = sum((Decimal(line["net_amount"] or "0") for line in lines), Decimal("0"))
    tax_total = (subtotal * Decimal("0.10")).quantize(Decimal("0.01"))
    grand_total = subtotal + tax_total + (Decimal("5.00") if ordinal == 3 else Decimal("0"))
    invoice_number = None if ordinal == 10 else f"SYN-{family_code}-{ordinal:04d}"
    series = None if ordinal == 9 else f"S{family_code[:3]}"
    invoice_tax_id = supplier_tax_id + ("-OTHER" if ordinal == 7 else "")
    po_number = f"PO-{family_code}-{ordinal:03d}"
    labels: dict[str, Any] = {
        "invoice_number": invoice_number,
        "invoice_series": series,
        "invoice_series_absent_confirmed": series is None,
        "invoice_date": f"2026-{(ordinal - 1) % 12 + 1:02d}-{(ordinal - 1) % 27 + 1:02d}",
        "supplier_tax_id": invoice_tax_id,
        "supplier_name": f"Synthetic Supplier {family_code} {ordinal:02d}",
        "po_number": po_number,
        "currency": currency,
        "subtotal": _fmt_decimal(subtotal),
        "tax_total": _fmt_decimal(tax_total),
        "grand_total": _fmt_decimal(grand_total),
        "discount_total": "0.00",
        "shipping_total": "0.00",
        "lines": lines,
    }
    po = {
        "po_number": po_number,
        "currency": currency,
        "state": "OPEN",
        "supplier_tax_id": supplier_tax_id,
        "lines": po_lines,
    }
    return InvoiceData.model_validate(labels).model_dump(mode="json"), po


def _line_text(line: dict[str, Any], labels: dict[str, str], language: str) -> str:
    values = [
        line.get("sku") or labels["none"],
        line["description"] or "",
        line["quantity"],
        line["unit"],
        _display_decimal(line["unit_price"], language),
        _display_decimal(line["net_amount"], language),
    ]
    return " | ".join(values)


def _page_content(
    family: str,
    language: str,
    labels: dict[str, Any],
    page_no: int,
    total_pages: int,
) -> tuple[list[str], dict[str, int]]:
    text = VI_LABELS if language.startswith("vi") else EN_LABELS
    bilingual = family == "dev_table_bilingual" or family == "test_multpage_mixed"
    header: list[str] = ["SYNTHETIC EVALUATION DATA - NOT A REAL INVOICE", text["invoice"]]
    if page_no > 1:
        header.append(f"{text['continued']} - {labels['invoice_number'] or text['none']}")

    def label(name: str) -> str:
        if not bilingual:
            return text[name]
        other = EN_LABELS[name] if language.startswith("vi") else VI_LABELS[name]
        return f"{text[name]} / {other}"

    field_lines: list[str] = []
    field_pages: dict[str, int] = {}
    if page_no == 1:
        for field, label_key in (
            ("invoice_number", "invoice_number"),
            ("invoice_series", "series"),
            ("invoice_date", "date"),
            ("supplier_tax_id", "tax_id"),
            ("supplier_name", "supplier"),
            ("po_number", "po"),
            ("currency", "currency"),
        ):
            value = labels.get(field)
            if value is None and field == "invoice_number":
                continue
            if value is None:
                value = text["none"]
            field_lines.append(f"{label(label_key)}: {value}")
            field_pages[field] = 1

    lines = labels["lines"]
    if total_pages == 1:
        selected_lines = list(enumerate(lines))
    else:
        pivot = max(1, (len(lines) + 1) // 2)
        selected_lines = list(enumerate(lines[:pivot])) if page_no == 1 else list(enumerate(lines[pivot:], start=pivot))
    line_lines = [
        f"{label('sku')} | {label('description')} | {label('quantity')} | {label('unit')} | "
        f"{label('unit_price')} | {label('net')}",
    ]
    for index, line in selected_lines:
        line_lines.append(_line_text(line, text, language))
        for field, value in line.items():
            if field != "line_id" and value is not None:
                field_pages[f"lines[{index}].{field}"] = page_no

    totals: list[str] = []
    if page_no == total_pages:
        for field, label_key in (
            ("subtotal", "subtotal"),
            ("tax_total", "tax"),
            ("discount_total", "discount"),
            ("shipping_total", "shipping"),
            ("grand_total", "total"),
        ):
            totals.append(f"{label(label_key)}: {_display_decimal(labels[field], language)} {labels['currency']}")
            field_pages[field] = page_no
    return header + field_lines + line_lines + totals, field_pages


def _render_pdf(pages: list[list[str]], output_path: Path, font_path: Path, scanned: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont(FONT_ALIASES[0], str(font_path)))
    pdfmetrics.registerFont(TTFont(FONT_ALIASES[1], str(font_path)))
    pdf = canvas.Canvas(str(output_path), pagesize=A4, invariant=1, pageCompression=1)
    pdf.setTitle(f"Synthetic evaluation invoice {output_path.stem}")
    pdf.setAuthor("IDP synthetic evaluation corpus generator")
    width, height = A4
    for page_no, lines in enumerate(pages, start=1):
        if scanned:
            image = Image.new("RGB", (1240, 1754), "white")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(str(font_path), 28)
            y = 68
            for line in lines:
                wrapped = simpleSplit(line, FONT_ALIASES[0], 28, 1110)
                for item in wrapped:
                    draw.text((70, y), item, fill=(28, 35, 33), font=font)
                    y += 43
            pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height, preserveAspectRatio=True)
        else:
            pdf.setFont(FONT_ALIASES[0], 10)
            y = height - 55
            for line in lines:
                for item in simpleSplit(line, FONT_ALIASES[0], 10, width - 90):
                    pdf.drawString(45, y, item)
                    y -= 23
        if page_no < len(pages):
            pdf.showPage()
    pdf.save()


def _expected_findings(labels: dict[str, Any], po: dict[str, Any]) -> list[str]:
    invoice = dict(labels)
    po_lines = []
    for index, line in enumerate(po["lines"], start=1):
        po_lines.append({**line, "id": f"po-line-{index}", "line_no": index})
    invoice_lines = []
    mapping: dict[str, str] = {}
    for index, line in enumerate(invoice["lines"], start=1):
        line_id = f"invoice-line-{index}"
        invoice_lines.append({**line, "line_id": line_id})
        if line.get("sku") and index <= len(po_lines) and po_lines[index - 1].get("sku") == line["sku"]:
            mapping[line_id] = po_lines[index - 1]["id"]
    invoice["lines"] = invoice_lines
    result = reconcile(invoice, {**po, "lines": po_lines}, mapping)
    return sorted({issue["code"] for issue in result["issues"]})


def build(font_path: Path) -> dict[str, Any]:
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    family_counts: Counter[str] = Counter()
    for split, family, family_language, layout in FAMILIES:
        for ordinal in range(1, 11):
            sample_id = f"syn-{family}-{ordinal:02d}"
            language = _language(family_language, ordinal)
            labels, po = _label_values(family, ordinal, language)
            total_pages = 2 if layout == "multipage" or ordinal in {4, 9} else 1
            scanned = layout == "ledger" or ordinal % 4 == 0
            evidence_pages: dict[str, int] = {}
            page_lines: list[list[str]] = []
            for page_no in range(1, total_pages + 1):
                lines, evidence = _page_content(family, language, labels, page_no, total_pages)
                page_lines.append(lines)
                evidence_pages.update(evidence)
            output_path = DOCUMENTS_DIR / f"{sample_id}.pdf"
            _render_pdf(page_lines, output_path, font_path, scanned)
            actual_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
            label_path = LABELS_DIR / f"{sample_id}.json"
            label_bytes = (json.dumps(labels, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            label_path.write_bytes(label_bytes)
            row = {
                "sample_id": sample_id,
                "source_document_id": sample_id,
                "file": output_path.relative_to(CORPUS_DIR).as_posix(),
                "sha256": actual_sha,
                "label_path": label_path.relative_to(CORPUS_DIR).as_posix(),
                "label_sha256": hashlib.sha256(label_bytes).hexdigest(),
                "source": "project-generated-synthetic",
                "license_or_consent": "synthetic generated data; no real invoice, vendor, or personal data",
                "synthetic": True,
                "label_status": "programmatic_reference_not_human_annotated",
                "layout_family": family,
                "split": split,
                "language": language,
                "modality": "scanned_pdf" if scanned else "native_pdf_text",
                "page_count": total_pages,
                "evidence_pages": evidence_pages,
                "po_reference": po,
                "expected_findings": _expected_findings(labels, po),
            }
            rows.append(row)
            family_counts[family] += 1

    manifest = {
        "schema_version": "1.0",
        "dataset_id": "synthetic-invoice-corpus-v1",
        "dataset_type": "ai_extraction_evaluation",
        "synthetic_only": True,
        "fixtures_included": False,
        "label_status": "programmatic synthetic reference labels; not human annotated",
        "generated_by": "scripts/build_synthetic_ai_corpus.py",
        "split_policy": {
            "dev": {"sample_count": 30, "layout_family_count": 3},
            "validation": {"sample_count": 20, "layout_family_count": 2},
            "test": {"sample_count": 40, "layout_family_count": 4},
            "grouping": "layout_family and source_document_id; no family, source document, or file hash may cross splits",
        },
        "samples": rows,
    }
    manifest_path = CORPUS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "manifest_path": manifest_path,
        "sample_count": len(rows),
        "split_counts": dict(Counter(row["split"] for row in rows)),
        "layout_family_counts": dict(sorted(family_counts.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic, synthetic AI evaluation invoices.")
    parser.add_argument("--font", help="Unicode TrueType font path; Arial or DejaVu Sans recommended")
    args = parser.parse_args()
    result = build(_font_path(args.font))
    print(
        f"synthetic_ai_corpus: {result['sample_count']} samples; splits={result['split_counts']}; "
        f"families={len(result['layout_family_counts'])}; manifest={result['manifest_path']}"
    )


if __name__ == "__main__":
    main()
