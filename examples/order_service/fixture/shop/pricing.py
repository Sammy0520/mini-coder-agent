from __future__ import annotations

from decimal import Decimal


def order_total(subtotal: Decimal, member: bool) -> Decimal:
    if member and subtotal > Decimal("50.00"):
        subtotal *= Decimal("0.90")
    return subtotal
