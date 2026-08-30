import unittest

from report import summarize, summarize_csv


class ReportTests(unittest.TestCase):
    def test_summarize_mixed_amount_types(self):
        result = summarize(
            [
                {"category": "food", "amount": "1.25"},
                {"category": "food", "amount": 2},
                {"category": "travel", "amount": 3.335},
            ]
        )
        self.assertEqual(result, {"food": 3.25, "travel": 3.33})

    def test_csv_reuses_behavior(self):
        text = "category,amount\nfood,1.20\n\nfood,2.30\ntravel,4\n"
        self.assertEqual(summarize_csv(text), {"food": 3.5, "travel": 4.0})

    def test_invalid_csv_fails(self):
        for text in ("name,amount\nx,1\n", "category,amount\nx,nope\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    summarize_csv(text)
