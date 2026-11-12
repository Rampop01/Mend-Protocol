"""
Example checkout service watched by Mend.

`calculate_order_total` has a real bug: a currency that is missing from the
rate table gets a rate of 0.0 and the division raises ZeroDivisionError. The
test suite in tests/ already describes the intended behaviour, so one test
fails until the bug is fixed.

Run this file while the Mend server is running to trigger the crash:

    python examples/payment_service/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mend.sdk import MendWatch

mend = MendWatch(repo="Rampop01/mend-protocol")

CURRENCY_RATES_TO_USD: Dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "NGN": 0.00063,
}


@mend
def calculate_order_total(amount_cents: int, currency: str, discount_code: str = "") -> float:
    """Converts an order amount to USD minor units and applies any discount."""
    rate = CURRENCY_RATES_TO_USD.get(currency.upper(), 0.0)

    usd_amount = amount_cents / rate

    if discount_code == "VIP10":
        usd_amount *= 0.90

    return round(usd_amount, 2)


if __name__ == "__main__":
    print(f"Order in USD: {calculate_order_total(5000, 'USD')}")
    print("Placing an order in a currency the service has no rate for...")
    try:
        calculate_order_total(5000, "XYZ")
    except ZeroDivisionError as exc:
        print(f"Checkout crashed: {type(exc).__name__}: {exc}")
        print("The crash was reported to Mend. Watch the dashboard at http://127.0.0.1:8770")
