from __future__ import annotations

import unittest
from decimal import Decimal

from shop.service import quote_order


class ServiceTests(unittest.TestCase):
    def test_public_api_stays_backward_compatible(self) -> None:
        total = quote_order([("25.00", "2")], member=True)
        self.assertEqual(total, Decimal("45.00"))

    def test_multiple_lines(self) -> None:
        total = quote_order([("12.50", "2"), ("5.00", "1")], member=False)
        self.assertEqual(total, Decimal("30.00"))


if __name__ == "__main__":
    unittest.main()
