from inventory import snapshot


def current_stock() -> dict[str, int]:
    return snapshot()
