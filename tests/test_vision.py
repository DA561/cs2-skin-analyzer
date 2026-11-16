import asyncio
import io
import json

import httpx
import pytest
from PIL import Image

from analyzer.providers import SourceError
from analyzer.vision import Vision


def test_vision_request_schema_and_output_validation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "vision-test-only")
    monkeypatch.setenv("VISION_MODEL", "fixture-model")
    im = io.BytesIO()
    Image.new("RGB", (20, 20)).save(im, format="PNG")
    item = {
        "name": "印花 | 测试 (金色)",
        "market_hash_name": None,
        "wear": None,
        "version": "normal",
        "pattern": None,
        "rank": 1,
        "screenshot_price": 150,
        "confidence": 0.99,
        "needs_review": False,
    }

    def handle(req):
        body = json.loads(req.content)
        assert body["store"] is False
        fmt = body["text"]["format"]
        assert fmt["strict"] is True
        assert "version" in fmt["schema"]["properties"]["items"]["items"]["required"]
        assert body["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"items": [item]})}]}
                ]
            },
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        "analyzer.vision.httpx.AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs)
    )
    r = asyncio.run(Vision().recognize(im.getvalue(), 30))
    assert r.items[0].rank == 1
    assert r.expected_count == 30
    item["version"] = "invented-version"
    with pytest.raises(SourceError, match="vision_invalid_structured_output"):
        asyncio.run(Vision().recognize(im.getvalue(), 30))
