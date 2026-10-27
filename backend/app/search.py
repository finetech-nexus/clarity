"""Field query used by the operational lists.

A query is free text plus field:value tokens, both required. This is the same
shape an analyst types in an Elasticsearch box. Clarity evaluates it on the
rows it already stores.
"""

import re


def _tokens(query: str) -> tuple[list[tuple[str, str]], list[str]]:
    filters: list[tuple[str, str]] = []
    words: list[str] = []
    for token in re.findall(r'"[^"]*"|\S+', query or ""):
        bare = token.strip('"')
        if not bare:
            continue
        if ":" in bare and not token.startswith('"'):
            field, value = bare.split(":", 1)
            if field and value:
                filters.append((field.lower(), value.lower()))
                continue
        words.append(bare.lower())
    return filters, words


def matches(query: str, fields: dict) -> bool:
    filters, words = _tokens(query)
    if not filters and not words:
        return True
    grouped: dict[str, list[str]] = {}
    for field, value in filters:
        grouped.setdefault(field, []).append(value)
    for field, values in grouped.items():
        haystack = str(fields.get(field, "")).lower()
        if not any(value in haystack for value in values):
            return False
    blob = " ".join(str(value) for value in fields.values()).lower()
    return all(word in blob for word in words)


def present(rows: list, query: str, fields_of, limit: int | None, offset: int | None):
    filtered = [row for row in rows if matches(query, fields_of(row))]
    if limit is None:
        return filtered
    size = min(max(int(limit), 1), 100)
    start = max(int(offset or 0), 0)
    return {"items": filtered[start : start + size], "total": len(filtered), "limit": size, "offset": start}
