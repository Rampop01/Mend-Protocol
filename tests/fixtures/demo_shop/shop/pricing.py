"""Prices a cart: item subtotal, coupon, shipping."""
from __future__ import annotations

from typing import Dict, List

CATALOG: Dict[str, int] = {  # price in cents
    "tee": 2500,
    "hoodie": 5500,
    "mug": 1500,
    "sticker": 300,
}

FREE_SHIPPING_FROM = 5000
SHIPPING = 700


def subtotal(items: List[Dict]) -> int:
    total = 0
    for item in items:
        total += CATALOG[item["sku"]] * int(item.get("qty", 1))
    return total


def coupon_discount(total: int, code: str) -> int:
    """Coupons look like SAVE10 or SAVE25: SAVE followed by a percentage."""
    if not code:
        return 0
    if not code.upper().startswith("SAVE"):
        return 0
    percent = int(code[4:])
    return total * min(percent, 50) // 100


def quote(items: List[Dict], coupon: str = "") -> Dict[str, int]:
    items_total = subtotal(items)
    discount = coupon_discount(items_total, coupon)
    after_discount = items_total - discount
    shipping = 0 if after_discount >= FREE_SHIPPING_FROM else SHIPPING
    return {
        "subtotal": items_total,
        "discount": discount,
        "shipping": shipping,
        "total": after_discount + shipping,
    }
