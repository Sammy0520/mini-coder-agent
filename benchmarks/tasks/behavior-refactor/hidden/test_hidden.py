import unittest
from dataclasses import FrozenInstanceError, is_dataclass

from formatter import format_line
from parser import Record, parse_record


class RefactorTests(unittest.TestCase):
    def test_record_is_frozen_dataclass(self):
        record = parse_record("Tea|2|yes")
        self.assertTrue(is_dataclass(record))
        self.assertIsInstance(record, Record)
        with self.assertRaises(FrozenInstanceError):
            record.count = 3

    def test_output_is_compatible(self):
        self.assertEqual(format_line(" Tea | 2 | yes "), "Tea x2 (enabled)")
        self.assertEqual(format_line("Coffee|0|no"), "Coffee x0 (disabled)")

    def test_invalid_inputs_still_fail(self):
        for value in ("bad", "|1|yes", "Tea|x|yes", "Tea|1|maybe"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_record(value)
