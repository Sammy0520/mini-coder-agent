# Todo CLI specification

Create `todo_cli.py` with these commands:

- `add TEXT --file PATH`: append an unfinished item and print its numeric id.
- `list --file PATH`: print one line per item as `ID [ ] TEXT` or `ID [x] TEXT`.
- `done ID --file PATH`: mark an item done; an unknown id exits non-zero with a useful error.

The file is JSON with a top-level `items` array. IDs start at 1 and never reuse an earlier id. Missing files behave as an empty list. The module must expose `main(argv=None) -> int` and must not execute on import.
