"""Turning messy user input into canonical host names."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


def normalize(raw: str) -> str | None:
    """Return the lowercase ASCII host of a domain or URL, or None if it is not one.

    >>> normalize("https://Loja.Example.com.br/produtos?x=1")
    'loja.example.com.br'
    >>> normalize("# comment") is None
    True
    """
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".")
    if "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host


def parse_lines(lines: Iterable[str]) -> list[str]:
    """Normalize every line, dropping blanks, comments, invalid entries and duplicates."""
    seen: dict[str, None] = {}
    for line in lines:
        host = normalize(line)
        if host is not None:
            seen.setdefault(host)
    return list(seen)
