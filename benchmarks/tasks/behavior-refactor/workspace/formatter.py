from parser import parse_record


def format_line(raw: str) -> str:
    record = parse_record(raw)
    state = "enabled" if record["enabled"] else "disabled"
    return f"{record['name']} x{record['count']} ({state})"
