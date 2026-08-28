from __future__ import annotations

import argparse

from .service import quote_order


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--member", action="store_true")
    parser.add_argument("price")
    parser.add_argument("quantity")
    args = parser.parse_args()
    print(quote_order([(args.price, args.quantity)], args.member))


if __name__ == "__main__":
    main()
