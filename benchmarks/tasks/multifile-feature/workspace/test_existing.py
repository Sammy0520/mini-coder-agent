import unittest

import inventory
from service import current_stock


class ExistingBehaviorTests(unittest.TestCase):
    def setUp(self):
        inventory.STOCK.clear()
        inventory.STOCK.update({"A": 5, "B": 3})

    def test_current_stock_is_a_copy(self):
        result = current_stock()
        result["A"] = 0
        self.assertEqual(inventory.STOCK["A"], 5)


if __name__ == "__main__":
    unittest.main()
