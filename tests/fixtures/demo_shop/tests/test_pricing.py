import unittest

from shop.pricing import coupon_discount, quote


class PricingTests(unittest.TestCase):
    def test_subtotal_and_shipping(self):
        self.assertEqual(quote([{"sku": "mug", "qty": 2}]), {"subtotal": 3000, "discount": 0, "shipping": 700, "total": 3700})

    def test_free_shipping_over_50(self):
        self.assertEqual(quote([{"sku": "hoodie"}])["shipping"], 0)

    def test_percentage_coupon(self):
        self.assertEqual(coupon_discount(10000, "SAVE10"), 1000)

    def test_coupon_is_capped_at_50_percent(self):
        self.assertEqual(coupon_discount(10000, "SAVE90"), 5000)

    def test_unknown_coupon_is_ignored(self):
        self.assertEqual(coupon_discount(10000, "WELCOME"), 0)

    def test_malformed_save_coupon_is_ignored(self):
        """Customers type things like SAVEBIG; the order must still go through at full price."""
        self.assertEqual(quote([{"sku": "tee"}], coupon="SAVEBIG")["total"], 3200)


if __name__ == "__main__":
    unittest.main()
