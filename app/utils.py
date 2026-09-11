from datetime import datetime, timezone


def as_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def first(d: dict, *keys, default=None):
    for key in keys:
        if isinstance(d, dict) and key in d and d[key] is not None:
            return d[key]
    return default


def iso_from_any(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def pct(x):
    return f"{x * 100:.2f}%" if x is not None else "n/a"


def short_addr(x):
    if not x:
        return "?"
    return f"{x[:6]}…{x[-4:]}"


def normalize_address(x):
    if not x:
        return None
    # Pendle asset IDs look like "42161-0xabc..."
    text = str(x)
    if "-" in text and text.split("-", 1)[0].isdigit():
        text = text.split("-", 1)[1]
    return text.lower()
