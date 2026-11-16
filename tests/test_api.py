import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from analyzer.jobs import Jobs
from analyzer.models import ItemsRequest, Snapshot
from analyzer.providers import Youpin
from analyzer.report import build_report
import main


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYZER_ACCESS_KEY", "test-only-access")
    monkeypatch.setenv("ANALYZER_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.delenv("CSQAQ_API_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VISION_MODEL", raising=False)
    main.app.middleware_stack = None
    with TestClient(main.app) as c:
        yield c


AUTH = {"X-Access-Key": "test-only-access"}
BODY = {"items": [{"name": "印花 | 测试 (全息)", "version": "normal"}], "expected_count": 30}


def test_auth_required_and_health_legacy(client):
    assert client.get("/health").json() == {"status": "ok"}
    for path in ["/api/capabilities", "/api/analyze/top30", "/api/jobs/" + "a" * 32]:
        assert client.get(path).status_code == 401
    assert client.post("/api/analyze/items", json=BODY).status_code == 401
    assert client.get("/api/capabilities", headers={"Authorization": "Bearer test-only-access"}).status_code == 200
    assert client.post("/api/csqaq/supply", headers=AUTH, json={"payload": {"id": 1}}).status_code == 503


def test_fail_closed_without_access_key(client, monkeypatch):
    monkeypatch.delenv("ANALYZER_ACCESS_KEY")
    assert client.post("/api/analyze/items", json=BODY, headers=AUTH).status_code == 503


def test_top30_does_not_substitute_another_rank(client):
    r = client.get("/api/analyze/top30", headers=AUTH)
    assert r.status_code == 503
    assert r.json()["detail"] == "youpin_today_gmv_top30_authorization_required"


def test_payload_limits_and_redaction(client):
    r = client.post("/api/analyze/items", headers=AUTH, json={"items": [], "token": "sensitive"})
    assert r.status_code == 422
    assert "sensitive" not in r.text
    assert client.post("/api/analyze/items", headers=AUTH, json={"items": BODY["items"] * 31}).status_code == 422
    assert (
        client.post(
            "/api/analyze/image", headers={**AUTH, "Content-Length": str(13 * 1024 * 1024)}, content=b"x"
        ).status_code
        == 413
    )


def test_image_validation_and_config(client):
    assert client.post("/api/analyze/image", headers=AUTH, content=b"not an image").status_code == 422
    f = io.BytesIO()
    Image.new("RGB", (32, 32)).save(f, format="PNG")
    r = client.post("/api/analyze/image", headers=AUTH, content=f.getvalue())
    assert r.json()["detail"] == "vision_not_configured"


def test_job_submission_cache_and_result(client):
    a = client.post("/api/analyze/items", headers=AUTH, json=BODY)
    assert a.status_code == 202
    job = a.json()["job_id"]
    b = client.post("/api/analyze/items", headers=AUTH, json=BODY)
    assert b.json()["job_id"] == job
    # Submit/get through HTTP; deterministic worker execution in app event loop.
    client.portal.call(main.app.state.jobs.run_one)
    r = client.get("/api/jobs/" + job, headers=AUTH).json()
    assert r["status"] == "completed"
    assert r["report"]["recognized_count"] == 1
    assert r["report"]["missing_rows"] == 29
    assert r["report"]["scored_count"] == 0
    assert r["report"]["ranking"] == []
    assert r["report"]["research_focus"] == []
    assert "csqaq_token_not_configured" in r["report"]["items"][0]["issues"]


def test_rate_limit(client):
    for _ in range(60):
        assert client.get("/api/capabilities", headers=AUTH).status_code == 200
    assert client.get("/api/capabilities", headers=AUTH).status_code == 429


def test_restart_marks_jobs_interrupted_and_owner_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYZER_DB_PATH", str(tmp_path / "db.sqlite"))
    j = Jobs(None, None, None)
    j.initialize()
    job = j.submit("owner", ItemsRequest.model_validate(BODY))["job_id"]
    assert j.get(job, "other") is None
    j.initialize()
    assert j.get(job, "owner")["status"] == "interrupted"


def test_queue_bound(tmp_path, monkeypatch):
    from analyzer.providers import SourceError

    monkeypatch.setenv("ANALYZER_DB_PATH", str(tmp_path / "db.sqlite"))
    j = Jobs(None, None, None)
    j.initialize()
    for i in range(8):
        j.submit("owner", ItemsRequest(items=[{"name": str(i), "version": "normal"}]))
    with pytest.raises(SourceError, match="job_queue_full"):
        j.submit("owner", ItemsRequest(items=[{"name": "ninth", "version": "normal"}]))


def test_full_list_partial_provider_report():
    class Fake:
        async def search(self, text):
            return [{"id": 1, "name": text, "market_hash_name": "Test"}]

        async def snapshot(self, identity):
            return Snapshot(), {"id": 1}

    req = ItemsRequest(items=[{"name": "印花 | 测试 (全息)", "version": "normal"}] * 30)
    r = asyncio.run(build_report(req, Fake(), Youpin()))
    assert len(r["items"]) == 30
    assert r["market_data_count"] == 0
    assert r["ranking"] == []
    assert sum("重复" in "".join(i["issues"]) for i in r["items"]) == 29


def test_image_job_structured_recognition_and_report(client, monkeypatch):
    class FakeVision:
        async def recognize(self, raw, expected_count):
            from analyzer.vision import validate_image

            assert validate_image(raw) == "image/png"
            return ItemsRequest(
                items=[
                    {
                        "name": "AK-47 | 红线 (久经沙场)",
                        "wear": "久经沙场",
                        "version": "normal",
                        "rank": 1,
                        "screenshot_price": 123,
                    }
                ],
                expected_count=expected_count,
                origin="chatgpt_vision",
            )

    monkeypatch.setenv("OPENAI_API_KEY", "test-vision-secret")
    monkeypatch.setenv("VISION_MODEL", "test-model")
    client.app.state.jobs.vision = FakeVision()
    f = io.BytesIO()
    Image.new("RGB", (32, 32)).save(f, format="PNG")
    r = client.post(
        "/api/analyze/image?expected_count=30", headers={**AUTH, "Content-Type": "image/png"}, content=f.getvalue()
    )
    assert r.status_code == 202
    job = r.json()["job_id"]
    client.portal.call(client.app.state.jobs.run_one)
    result = client.get("/api/jobs/" + job, headers=AUTH).json()
    assert result["status"] == "completed"
    assert result["report"]["items"][0]["input"]["screenshot_price"] == 123
    assert result["report"]["missing_rows"] == 29
    assert result["report"]["scored_count"] == 0
    assert not client.app.state.jobs.images


def test_actions_schema_is_valid_and_public(client):
    schema = client.get("/api/actions-schema").json()
    assert schema["components"]["securitySchemes"]["AnalyzerKey"]["scheme"] == "bearer"
    assert "/api/analyze/items" in schema["paths"]


def test_mcp_is_authenticated_and_lists_tools(client):
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    r = client.post("/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    assert r.json()["result"]["tools"][0]["name"] == "get_analyzer_capabilities"


def test_mcp_calls_capabilities_without_secret_exposure(client):
    r = client.post(
        "/mcp",
        headers=AUTH,
        json={"jsonrpc": "2.0", "id": "capabilities", "method": "tools/call", "params": {"name": "get_analyzer_capabilities"}},
    )
    content = r.json()["result"]["content"][0]["text"]
    assert "csqaq_token_configured" in content
    assert "test-only-access" not in content


def test_old_manual_score_contract_remains(client):
    r = client.post(
        "/api/score",
        json={"liquidity": 25, "trend": 20, "valuation": 20, "profit_space": 20, "supply_demand": 10, "market_risk": 5},
    )
    assert r.status_code == 200
    assert r.json()["score"] == 100
    assert "人工" in r.json()["note"]


def test_chunked_body_limit_without_content_length():
    from analyzer.security import Guard, MAX_BODY

    events = []
    called = []

    async def app(scope, receive, send):
        called.append(True)

    async def receive():
        return {"type": "http.request", "body": b"x" * (MAX_BODY + 1), "more_body": False}

    async def send(event):
        events.append(event)

    asyncio.run(Guard(app)({"type": "http", "path": "/api/score", "method": "POST", "headers": []}, receive, send))
    assert events[0]["status"] == 413
    assert not called
