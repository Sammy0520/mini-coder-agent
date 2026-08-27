def final_price(price: float, discount_percent: float) -> float:
    """Return price after a percentage discount."""
    if price < 0:
        raise ValueError("price must not be negative")
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("discount_percent must be between 0 and 100")
    return round(price * (1 - discount_percent / 100), 2)

