import json
import os
import unittest

from invoice import invoice_total, line_total

HERE = os.path.dirname(os.path.abspath(__file__))


class LineTotalTest(unittest.TestCase):
    def test_no_discount(self):
        self.assertEqual(line_total(3, 2.50), 7.50)

    def test_ten_percent_discount(self):
        # discount_pct is a percentage: 10 means 10%.
        self.assertEqual(line_total(4, 25.00, 10), 90.00)


class InvoiceTotalTest(unittest.TestCase):
    def test_sample_invoice(self):
        # Reads the shared sample invoice. It lives outside this repo and
        # is only present on the billing team's machines.
        with open(os.path.join(HERE, "..", "billing-fixtures", "sample_invoice.json")) as f:
            sample = json.load(f)
        self.assertEqual(invoice_total(sample["lines"]), sample["expected_total"])


if __name__ == "__main__":
    unittest.main()
