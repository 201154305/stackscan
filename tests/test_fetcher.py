import socket

import httpx
import pytest
import respx

from stackscan.fetcher import Fetcher


@pytest.fixture
async def client():
    async with httpx.AsyncClient(follow_redirects=True) as c:
        yield c


@respx.mock
async def test_collects_status_headers_cookies_and_body(client):
    respx.get("https://shop.test/robots.txt").respond(404)
    respx.get("https://shop.test/").respond(
        200,
        html="<html><body>oi</body></html>",
        headers=[
            ("Server", "nginx/1.25.3"),
            ("Set-Cookie", "PHPSESSID=abc; Path=/"),
            ("Set-Cookie", "lang=pt; Path=/"),
            ("X-Request-Id", "123"),
        ],
    )
    result = await Fetcher(client).fetch("shop.test")
    assert result.error is None
    assert result.status == 200
    assert "oi" in result.body
    assert result.cookies == ["PHPSESSID", "lang"]
    assert result.headers["server"] == "nginx/1.25.3"
    assert "x-request-id" not in result.headers  # not stored...
    assert result.all_headers["x-request-id"] == "123"  # ...but still used for detection
    assert "set-cookie" not in result.all_headers


@respx.mock
async def test_respects_robots_txt(client):
    respx.get("https://private.test/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    home = respx.get("https://private.test/").respond(200)
    result = await Fetcher(client).fetch("private.test")
    assert result.error_kind == "robots"
    assert not result.retryable
    assert not home.called


@respx.mock
async def test_falls_back_to_http_when_https_cannot_connect(client):
    respx.get("https://old.test/robots.txt").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://old.test/").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("http://old.test/robots.txt").respond(404)
    respx.get("http://old.test/").respond(200, html="ok")
    result = await Fetcher(client).fetch("old.test")
    assert result.error is None
    assert result.url == "http://old.test/"


async def test_unknown_host_is_final_and_does_not_fall_back():
    # MockTransport keeps the exception chain intact, as a real connection does.
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        error = httpx.ConnectError("name resolution failed", request=request)
        error.__cause__ = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Fetcher(client).fetch("gone.test")
    assert result.error_kind == "dns"
    assert not result.retryable
    assert "http://gone.test/" not in requested


@respx.mock
async def test_timeout_is_retryable_and_does_not_fall_back(client):
    respx.get("https://slow.test/robots.txt").respond(404)
    respx.get("https://slow.test/").mock(side_effect=httpx.ReadTimeout("too slow"))
    plain = respx.get("http://slow.test/").respond(200)
    result = await Fetcher(client).fetch("slow.test")
    assert result.error_kind == "timeout"
    assert result.retryable
    assert not plain.called


@respx.mock
async def test_body_is_capped(client):
    respx.get("https://big.test/robots.txt").respond(404)
    respx.get("https://big.test/").respond(200, content=b"x" * 10_000)
    result = await Fetcher(client, max_bytes=100).fetch("big.test")
    assert result.body_bytes == 100
    assert len(result.body) == 100
