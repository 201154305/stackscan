"""Polite HTTP fetching of a domain's home page."""

from __future__ import annotations

import socket
import time
import urllib.robotparser
from dataclasses import dataclass, field

import httpx

from stackscan import __version__

USER_AGENT = f"stackscan/{__version__} (homepage-only technology survey)"
ROBOTS_TOKEN = "stackscan"
# Headers worth keeping in the database. Everything else (cookies, CSP
# policies, tracking ids) is either sensitive or just noise.
KEPT_HEADERS = frozenset(
    {
        "server",
        "x-powered-by",
        "via",
        "x-cache",
        "content-type",
        "content-language",
        "x-generator",
        "x-aspnet-version",
        "x-drupal-cache",
        "x-varnish",
        "cf-ray",
        "x-amz-cf-id",
        "x-vercel-id",
        "x-nf-request-id",
        "x-fastly-request-id",
        "x-shopid",
        "x-shopify-stage",
        "x-wix-request-id",
        "x-akamai-transformed",
        "strict-transport-security",
        "alt-svc",
        "link",
    }
)


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    elapsed_ms: int | None = None
    body: str = ""
    body_bytes: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    all_headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None  # connect | dns | timeout | protocol | robots | other

    @property
    def retryable(self) -> bool:
        return self.error_kind in {"connect", "timeout", "protocol"}


def make_client(*, concurrency: int, timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
    )


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_bytes: int = 2_000_000,
        respect_robots: bool = True,
    ) -> None:
        self.client = client
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots

    async def fetch(self, domain: str) -> FetchResult:
        """Fetch https://domain/, falling back to http:// only when HTTPS cannot connect."""
        result = FetchResult(url=f"https://{domain}/")
        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}/"
            if self.respect_robots and not await self._robots_allows(scheme, domain):
                return FetchResult(url=url, error="disallowed by robots.txt", error_kind="robots")
            result = await self._get(url)
            if result.error_kind != "connect":
                return result
        return result

    async def _robots_allows(self, scheme: str, domain: str) -> bool:
        """Missing or unreachable robots.txt means allowed, as most crawlers treat it."""
        try:
            resp = await self.client.get(f"{scheme}://{domain}/robots.txt", timeout=10.0)
        except httpx.HTTPError:
            return True
        if resp.status_code != 200:
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser.can_fetch(ROBOTS_TOKEN, f"{scheme}://{domain}/")

    async def _get(self, url: str) -> FetchResult:
        started = time.perf_counter()

        def elapsed() -> int:
            return round((time.perf_counter() - started) * 1000)

        try:
            async with self.client.stream("GET", url) as resp:
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.max_bytes:
                        break
                raw = b"".join(chunks)[: self.max_bytes]
                names = {k.lower() for k in resp.headers}
                all_headers = {n: ", ".join(resp.headers.get_list(n)) for n in names if n != "set-cookie"}
                return FetchResult(
                    url=url,
                    final_url=str(resp.url),
                    status=resp.status_code,
                    elapsed_ms=elapsed(),
                    body=_decode(raw, resp.charset_encoding),
                    body_bytes=len(raw),
                    headers={k: v for k, v in all_headers.items() if k in KEPT_HEADERS},
                    all_headers=all_headers,
                    cookies=[c.split("=", 1)[0].strip() for c in resp.headers.get_list("set-cookie")],
                )
        except httpx.TimeoutException as exc:
            return _failure(url, elapsed(), "timeout", exc)
        except httpx.ConnectError as exc:
            kind = "dns" if _is_unknown_host(exc) else "connect"
            return _failure(url, elapsed(), kind, exc)
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            return _failure(url, elapsed(), "protocol", exc)
        except httpx.HTTPError as exc:
            return _failure(url, elapsed(), "other", exc)


def _decode(raw: bytes, encoding: str | None) -> str:
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _is_unknown_host(exc: BaseException) -> bool:
    """True when DNS says the name does not exist: retrying will not help.

    Temporary resolver failures (EAI_AGAIN) are left as ordinary, retryable
    connect errors. The resolver error sits somewhere in the exception chain.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, socket.gaierror) and seen.errno == socket.EAI_NONAME:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _failure(url: str, elapsed_ms: int, kind: str, exc: Exception) -> FetchResult:
    message = str(exc) or type(exc).__name__
    return FetchResult(url=url, elapsed_ms=elapsed_ms, error=f"{kind}: {message}"[:500], error_kind=kind)
