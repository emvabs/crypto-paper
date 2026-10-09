import httpx
import pytest

from src.okx_client import OKXClient, OKXError


def client(handler, hosts=("https://a", "https://b")):
    c = OKXClient(hosts[0], hosts[1] if len(hosts) > 1 else None, backoff_seconds=0)
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_server_errors_are_retried_then_the_fallback_host_answers():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "a":
            return httpx.Response(503)
        return httpx.Response(200, json={"code": "0", "data": [1]})

    assert client(handler)._get("/x", {}) == [1]
    assert calls == ["a", "a", "a", "b"]


def test_request_errors_skip_straight_to_the_next_host():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "a":
            return httpx.Response(200, json={"code": "51001", "msg": "no such instrument"})
        return httpx.Response(404)

    with pytest.raises(OKXError, match="HTTP 404"):
        client(handler)._get("/x", {})
    assert calls == ["a", "b"]
