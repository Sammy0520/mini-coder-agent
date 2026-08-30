class OutOfStockError(ValueError):
    pass


STOCK = {"A": 5, "B": 3}


def snapshot() -> dict[str, int]:
    return dict(STOCK)
