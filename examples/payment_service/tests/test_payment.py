"""Tests for the example checkout service."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calculate_order_total


class TestPaymentService(unittest.TestCase):
    def test_standard_usd(self):
        self.assertEqual(calculate_order_total(1000, "USD"), 1000.0)

    def test_vip_discount(self):
        self.assertEqual(calculate_order_total(1000, "USD", discount_code="VIP10"), 900.0)

    def test_eur_conversion(self):
        self.assertAlmostEqual(calculate_order_total(1080, "EUR"), 1000.0, places=1)

    def test_unknown_currency_does_not_crash(self):
        """A currency with no rate must yield 0.0, not an unhandled ZeroDivisionError."""
        self.assertEqual(calculate_order_total(1000, "XYZ"), 0.0)


if __name__ == "__main__":
    unittest.main()
