from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

VND_QUANTUM = Decimal("1")
MAX_SCALE = 6


def parse_decimal(value: str | int | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def fits_numeric_20_6(value: Decimal) -> bool:
    """Return whether a finite Decimal fits PostgreSQL NUMERIC(20,6) without rounding."""
    if not value.is_finite():
        return False
    parts = value.as_tuple()
    scale = max(0, -parts.exponent)
    integer_digits = max(0, value.adjusted() + 1) if value else 0
    return scale <= MAX_SCALE and integer_digits <= 20 - MAX_SCALE


def round_vnd(value: Decimal) -> Decimal:
    return value.quantize(VND_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
