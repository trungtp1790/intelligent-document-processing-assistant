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


def round_vnd(value: Decimal) -> Decimal:
    return value.quantize(VND_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
