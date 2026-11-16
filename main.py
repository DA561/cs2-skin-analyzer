import os
import time
import threading
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="CS2 Skin Analyzer", version="0.1.0")
BASE_URL = "https://api.csqaq.com/api/v1"
_lock = threading.Lock()
_last_request = 0.0


class ScoreRequest(BaseModel):
    liquidity: float = Field(ge=0, le=25)
    trend: float = Field(ge=0, le=20)
    valuation: float = Field(ge=0, le=20)
    profit_space: float = Field(ge=0, le=20)
    supply_demand: float = Field(ge=0, le=10)
    market_risk: float = Field(ge=0, le=5)


class UpstreamRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@app.get("/")
def index():
    return {"name": "CS2 Skin Analyzer", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def status():
    return {"csqaq_configured": bool(os.getenv("CSQAQ_API_TOKEN")),
            "proxy_access_configured": bool(os.getenv("ANALYZER_ACCESS_KEY"))}


@app.post("/api/score")
def score(data: ScoreRequest):
    total = round(sum(data.model_dump().values()), 1)
    tier = "优先关注" if total >= 85 else "重点关注" if total >= 75 else "观察" if total >= 65 else "暂不买入"
    return {"score": total, "tier": tier, "components": data.model_dump(),
            "note": "人工输入的评分，不代表实时行情或买入信号"}


ALLOWED = {
    "rank": "info/get_rank_list",
    "supply": "info/good/statistic",
    "volume": "info/vol_data_detail",
}


@app.post("/api/csqaq/{kind}")
def csqaq(kind: str, body: UpstreamRequest, x_access_key: str | None = Header(default=None)):
    if kind not in ALLOWED:
        raise HTTPException(404, "Unsupported endpoint")
    access_key = os.getenv("ANALYZER_ACCESS_KEY", "")
    if not access_key:
        raise HTTPException(503, "Set ANALYZER_ACCESS_KEY in Railway before enabling proxy")
    import secrets
    if not x_access_key or not secrets.compare_digest(x_access_key, access_key):
        raise HTTPException(401, "Invalid access key")
    token = os.getenv("CSQAQ_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "CSQAQ_API_TOKEN not configured")
    # This process-level rate limit is sufficient for a single worker; multi-instance deployments
    # must use a shared rate limiter or restrict to one instance.
    global _last_request
    with _lock:
        wait = 1.1 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            with httpx.Client(timeout=20) as client:
                result = client.post(f"{BASE_URL}/{ALLOWED[kind]}", json=body.payload,
                                     headers={"ApiToken": token})
        except httpx.TimeoutException:
            raise HTTPException(504, "Upstream timeout")
        except httpx.HTTPError:
            raise HTTPException(502, "Upstream connection error")
    if result.status_code == 429:
        raise HTTPException(429, "Upstream rate limit")
    if result.status_code >= 400:
        raise HTTPException(502, f"Upstream HTTP {result.status_code}")
    try:
        return result.json()
    except ValueError:
        raise HTTPException(502, "Upstream response was not JSON")
