import asyncio
import json

import httpx
import pytest

from analyzer.providers import CSQAQ, SourceError


def run(coro):
    return asyncio.run(coro)


def provider(monkeypatch, handler):
    monkeypatch.setenv("CSQAQ_API_TOKEN", "never-print-this-token")
    c = CSQAQ(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c.interval = 0  # Test clock only; production constructor enforces >=1.1 sec.
    return c


@pytest.mark.parametrize(
    "status,body,code",
    [
        (200, {"code": 401, "msg": "secret-value"}, "csqaq_auth_or_ip_whitelist_rejected"),
        (403, {}, "csqaq_auth_or_ip_whitelist_rejected"),
        (429, {}, "csqaq_rate_limited"),
        (200, {"code": 500, "msg": "secret-value"}, "csqaq_business_error"),
        (200, [], "csqaq_invalid_schema"),
        (502, {}, "csqaq_http_error"),
    ],
)
def test_errors_sanitized(monkeypatch, status, body, code):
    c = provider(monkeypatch, lambda r: httpx.Response(status, json=body, headers={"Retry-After": "120"}))
    with pytest.raises(SourceError) as exc:
        run(c.request("GET", "info/good"))
    assert str(exc.value) == code
    assert "secret-value" not in str(exc.value)
    if status == 429:
        assert exc.value.retry_after == 120


def test_timeout(monkeypatch):
    def handler(r):
        raise httpx.ReadTimeout("sensitive details")

    c = provider(monkeypatch, handler)
    with pytest.raises(SourceError, match="csqaq_timeout"):
        run(c.request("GET", "info/good"))


def test_cache_keeps_original_query_time(monkeypatch):
    count = []

    def handler(r):
        count.append(1)
        assert r.headers["ApiToken"] == "never-print-this-token"
        return httpx.Response(200, json={"code": 200, "data": {}})

    c = provider(monkeypatch, handler)

    async def scenario():
        a = await c.request("GET", "info/good", {"id": 1})
        b = await c.request("GET", "info/good", {"id": 1})
        assert a["_queried_at"] == b["_queried_at"]
        assert len(count) == 1

    run(scenario())


def test_youpin_fields_not_steam_volume(monkeypatch):
    def handler(r):
        if r.url.path.endswith("/good"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "goods_info": {
                            "id": 1,
                            "name": "test",
                            "market_hash_name": "Test",
                            "yyyp_sell_price": 123,
                            "yyyp_buy_price": 120,
                            "yyyp_sell_num": 20,
                            "yyyp_buy_num": 10,
                            "turnover_number": 9999,
                            "updated_at": "2026-11-16T12:00:00",
                        }
                    },
                },
            )
        if r.url.path.endswith("/statistic"):
            return httpx.Response(200, json={"code": 200, "data": []})
        payload = json.loads(r.content)
        assert payload["platform"] == 2
        assert payload["key"] != "turnover_number"
        return httpx.Response(200, json={"code": 200, "data": {"timestamp": [1760000000000], "main_data": [100]}})

    c = provider(monkeypatch, handler)
    s, aux = run(c.snapshot({"id": 1, "name": "test", "market_hash_name": "Test"}))
    assert s.fields["ask"].value == 123
    assert s.fields["ask"].observed_at is None
    assert "volume_24h" not in s.fields
    assert "turnover_number" not in aux


def test_chart_misalignment_not_zipped_silently(monkeypatch):
    def handler(r):
        if r.url.path.endswith("/good"):
            return httpx.Response(
                200, json={"code": 200, "data": {"goods_info": {"id": 1, "name": "test", "market_hash_name": "Test"}}}
            )
        if r.url.path.endswith("/statistic"):
            return httpx.Response(200, json={"code": 200, "data": []})
        return httpx.Response(
            200, json={"code": 200, "data": {"timestamp": [1760000000000, 1760000001000], "main_data": [100]}}
        )

    c = provider(monkeypatch, handler)
    s, _ = run(c.snapshot({"id": 1, "name": "test", "market_hash_name": "Test"}))
    assert "price_history" not in s.series
    assert any("unaligned" in x for x in s.issues)


@pytest.mark.parametrize("data", [None, [], {"data": []}, {"data": {"bad": None}}])
def test_malformed_catalog_never_produces_guessed_matches(monkeypatch, data):
    c = provider(monkeypatch, lambda r: httpx.Response(200, json={"code": 200, "data": data}))
    with pytest.raises(SourceError, match="csqaq_search_schema_changed"):
        run(c.search("test"))


def test_real_request_spacing_obeys_official_limit(monkeypatch):
    import time

    monkeypatch.setenv("CSQAQ_API_TOKEN", "test-token-only")
    times = []

    def handler(r):
        times.append(time.monotonic())
        return httpx.Response(200, json={"code": 200, "data": {}})

    c = CSQAQ(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def scenario():
        await asyncio.gather(c.request("GET", "info/good", {"id": 1}), c.request("GET", "info/good", {"id": 2}))

    run(scenario())
    assert times[1] - times[0] >= 1.09
