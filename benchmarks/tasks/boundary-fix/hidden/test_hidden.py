import unittest

from pricing import discount_rate


class HiddenPricingTests(unittest.TestCase):
    def test_exact_threshold(self):
        self.assertEqual(discount_rate(500), 0.10)

    def test_zero_and_large_values(self):
        self.assertEqual(discount_rate(0), 0.0)
        self.assertEqual(discount_rate(10_000), 0.10)
