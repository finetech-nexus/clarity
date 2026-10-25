from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_dt(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_go_duration(value: str) -> timedelta:
    """Parse a subset of Go time.ParseDuration: hours, minutes and seconds."""
    if not value or not isinstance(value, str):
        raise ValueError("invalid duration")
    total = 0
    number = ""
    units = {"h": 3600, "m": 60, "s": 1}
    for char in value.strip():
        if char.isdigit() or char == ".":
            number += char
            continue
        if char not in units or not number:
            raise ValueError("invalid duration")
        total += float(number) * units[char]
        number = ""
    if number or total <= 0:
        raise ValueError("invalid duration")
    if total > 180 * 24 * 3600:
        raise ValueError("duration exceeds 180 days")
    return timedelta(seconds=total)
