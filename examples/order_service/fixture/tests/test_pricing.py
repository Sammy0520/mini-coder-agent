from __future__ import annotations

import unittest
from decimal import Decimal

from shop.policy import DiscountPolicy
from shop.pricing import order_total


class PricingTests(unittest.TestCase):
    def test_member_qualifies_at_exact_threshold(self) -> None:
        policy = DiscountPolicy(member=True)
        self.assertEqual(order_total(Decimal("50.00"), policy), Decimal("45.00"))

    def test_non_member_does_not_receive_discount(self) -> None:
        policy = DiscountPolicy(member=False)
        self.assertEqual(order_total(Decimal("80.00"), policy), Decimal("80.00"))

    def test_money_is_rounded_to_two_places(self) -> None:
        policy = DiscountPolicy(member=True)
        self.assertEqual(order_total(Decimal("55.55"), policy), Decimal("50.00"))


if __name__ == "__main__":
    unittest.main()
