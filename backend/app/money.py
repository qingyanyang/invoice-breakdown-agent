"""Shared monetary primitives used by deterministic workbook stages."""

from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def amount_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
