# Task

Replace the hard-coded member discount behavior with a reusable
`DiscountPolicy` dataclass in `shop/policy.py`.

Requirements:

1. A member gets a 10% discount when the subtotal is **at least** 50.00.
2. A non-member never receives the member discount.
3. `shop.pricing.order_total` must accept a `DiscountPolicy` rather than a
   boolean flag.
4. Keep the public `shop.service.quote_order(lines, member)` API unchanged by
   constructing and passing the policy there.
5. Money must be rounded to two decimal places.
6. Do not edit tests. Run the full test suite.
