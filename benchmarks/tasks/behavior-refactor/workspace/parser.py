def parse_record(raw: str) -> dict[str, object]:
    parts = raw.split("|")
    if len(parts) != 3:
        raise ValueError("expected name|count|enabled")
    name = parts[0].strip()
    count = int(parts[1])
    enabled_text = parts[2].strip().lower()
    if not name or enabled_text not in {"yes", "no"}:
        raise ValueError("invalid record")
    return {"name": name, "count": count, "enabled": enabled_text == "yes"}
