import unittest

from discount import final_price


class DiscountTests(unittest.TestCase):
    def test_twenty_percent_discount(self) -> None:
        self.assertEqual(final_price(100, 20), 80)

    def test_full_discount(self) -> None:
        self.assertEqual(final_price(50, 100), 0)

    def test_rejects_discount_above_one_hundred(self) -> None:
        with self.assertRaises(ValueError):
            final_price(100, 101)


if __name__ == "__main__":
    unittest.main()

