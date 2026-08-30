import unittest

import inventory
from inventory import OutOfStockError, reserve
from service import reserve_order


class ReservationTests(unittest.TestCase):
    def setUp(self):
        inventory.STOCK.clear()
        inventory.STOCK.update({"A": 5, "B": 3})

    def test_single_reservation(self):
        self.assertEqual(reserve("A", 2), 3)
        self.assertEqual(inventory.STOCK["A"], 3)

    def test_order_is_atomic(self):
        with self.assertRaises(OutOfStockError):
            reserve_order([("A", 2), ("B", 9)])
        self.assertEqual(inventory.STOCK, {"A": 5, "B": 3})

    def test_order_combines_duplicate_lines(self):
        result = reserve_order([("A", 2), ("A", 1), ("B", 1)])
        self.assertEqual(result, {"A": 2, "B": 2})

    def test_invalid_quantity_does_not_mutate(self):
        with self.assertRaises(ValueError):
            reserve_order([("A", 0)])
        self.assertEqual(inventory.STOCK, {"A": 5, "B": 3})
