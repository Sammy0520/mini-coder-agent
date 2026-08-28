from __future__ import annotations

from decimal import Decimal

from .pricing import order_total


def quote_order(lines: list[tuple[str, str]], member: bool) -> Decimal:
    subtotal = sum(
        (Decimal(price) * int(quantity) for price, quantity in lines),
        start=Decimal("0.00"),
    )
    return order_total(subtotal, member)
