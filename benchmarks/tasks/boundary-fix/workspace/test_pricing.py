import unittest

from pricing import discount_rate


class PricingTests(unittest.TestCase):
    def test_above_threshold(self):
        self.assertEqual(discount_rate(501), 0.10)

    def test_below_threshold(self):
        self.assertEqual(discount_rate(499), 0.0)


if __name__ == "__main__":
    unittest.main()
