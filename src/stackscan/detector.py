"""Technology detection from a fetched page.

Each fingerprint says where to look (response headers, cookie names,
<meta> tags, <script src> URLs, raw HTML) and what regex to match.
A capture group, when present, is taken as the version.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from importlib import resources
from typing import Any

EVIDENCE_MAX = 120


@dataclass(frozen=True)
class Detection:
    technology: str
    category: str
    version: str | None
    evidence: str


@dataclass
class Page:
    html: str = ""
    headers: dict[str, str] = field(default_factory=dict)  # lowercase names
    cookies: list[str] = field(default_factory=list)  # cookie names only


@dataclass
class _Rule:
    name: str
    category: str
    headers: dict[str, re.Pattern[str]]
    cookies: list[re.Pattern[str]]
    meta: dict[str, re.Pattern[str]]
    scripts: list[re.Pattern[str]]
    html: list[re.Pattern[str]]
    implies: list[str]


class _TagCollector(HTMLParser):
    """Collects <script src> and <meta name|property content> without a full DOM."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): v or "" for k, v in attrs}
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"])
        elif tag == "meta" and "content" in values:
            name = (values.get("name") or values.get("property") or "").lower()
            if name:
                self.meta.setdefault(name, values["content"])


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _version(match: re.Match[str]) -> str | None:
    if match.groups() and match.group(1):
        return match.group(1)
    return None


def _evidence(where: str, text: str) -> str:
    text = " ".join(text.split())
    if len(text) > EVIDENCE_MAX:
        text = text[: EVIDENCE_MAX - 1] + "…"
    return f"{where}: {text}"


class Detector:
    def __init__(self, fingerprints: list[dict[str, Any]]) -> None:
        self.rules: list[_Rule] = []
        for fp in fingerprints:
            self.rules.append(
                _Rule(
                    name=fp["name"],
                    category=fp["category"],
                    headers={k.lower(): _compile(v) for k, v in fp.get("headers", {}).items()},
                    cookies=[_compile(p) for p in _as_list(fp.get("cookies"))],
                    meta={k.lower(): _compile(v) for k, v in fp.get("meta", {}).items()},
                    scripts=[_compile(p) for p in _as_list(fp.get("scripts"))],
                    html=[_compile(p) for p in _as_list(fp.get("html"))],
                    implies=_as_list(fp.get("implies")),
                )
            )
        self.by_name = {rule.name: rule for rule in self.rules}
        unknown = {i for r in self.rules for i in r.implies} - self.by_name.keys()
        if unknown:
            raise ValueError(f"fingerprints imply unknown technologies: {sorted(unknown)}")

    @classmethod
    def default(cls) -> Detector:
        data = resources.files("stackscan").joinpath("fingerprints.json").read_text("utf-8")
        return cls(json.loads(data))

    def detect(self, page: Page) -> list[Detection]:
        tags = _TagCollector()
        try:
            tags.feed(page.html)
            tags.close()
        except Exception:  # broken HTML must never stop a crawl
            pass

        found: dict[str, Detection] = {}
        for rule in self.rules:
            hit = self._match(rule, page, tags)
            if hit is not None:
                found[rule.name] = hit

        pending = list(found.values())
        while pending:
            source = pending.pop()
            for implied in self.by_name[source.technology].implies:
                if implied not in found:
                    rule = self.by_name[implied]
                    found[implied] = Detection(
                        rule.name, rule.category, None, f"implied by {source.technology}"
                    )
                    pending.append(found[implied])

        return sorted(found.values(), key=lambda d: (d.category, d.technology))

    @staticmethod
    def _match(rule: _Rule, page: Page, tags: _TagCollector) -> Detection | None:
        def hit(where: str, text: str, match: re.Match[str]) -> Detection:
            return Detection(rule.name, rule.category, _version(match), _evidence(where, text))

        for name, pattern in rule.headers.items():
            value = page.headers.get(name)
            if value is not None and (m := pattern.search(value)):
                return hit(f"header {name}", value or "(present)", m)
        for pattern in rule.cookies:
            for cookie in page.cookies:
                if m := pattern.search(cookie):
                    return hit("cookie", cookie, m)
        for name, pattern in rule.meta.items():
            value = tags.meta.get(name)
            if value is not None and (m := pattern.search(value)):
                return hit(f"meta {name}", value, m)
        for pattern in rule.scripts:
            for src in tags.scripts:
                if m := pattern.search(src):
                    return hit("script", src, m)
        for pattern in rule.html:
            if m := pattern.search(page.html):
                return hit("html", m.group(0), m)
        return None
