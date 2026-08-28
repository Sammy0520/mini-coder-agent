# Demo instructions

- Treat `tests/` as the acceptance contract; do not edit tests.
- Keep `shop.service.quote_order(lines, member)` backward compatible.
- Use `Decimal` for money and return values rounded to two decimal places.
- Run `python -m unittest discover -s tests -v` before reporting completion.
