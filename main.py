import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from analyzer.jobs import Jobs
from analyzer.models import ItemsRequest
from analyzer.providers import CSQAQ, SourceError, Youpin
from analyzer.security import Guard
from analyzer.vision import Vision, validate_image

csqaq_provider = CSQAQ()
youpin_provider = Youpin()
vision_provider = Vision()


@asynccontextmanager
async def lifespan(app):
    if int(os.getenv("WEB_CONCURRENCY", "1")) != 1 or int(os.getenv("RAILWAY_REPLICA_COUNT", "1")) != 1:
        raise RuntimeError("Use one worker and one replica; shared distributed limiter not configured")
    jobs = Jobs(csqaq_provider, youpin_provider, vision_provider)
    jobs.initialize()
    app.state.jobs = jobs
    task = asyncio.create_task(jobs.worker())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CS2 Skin Analyzer", version="0.2.0", lifespan=lifespan)
app.add_middleware(Guard)


MCP_TOOLS = [
    {
        "name": "get_analyzer_capabilities",
        "description": "Check configured CS2 market-analysis capabilities. This does not prove live upstream authorization.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "analyze_items",
        "description": "Submit all visible screenshot items for objective market analysis. Do not invent unseen rows.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object"}},
                "expected_count": {"type": "integer", "minimum": 1, "maximum": 30},
                "origin": {"type": "string", "enum": ["item_list", "chatgpt_vision"]},
            },
        },
    },
    {
        "name": "get_analysis_job",
        "description": "Get the status and structured report for an analysis job.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["job_id"],
            "properties": {"job_id": {"type": "string", "minLength": 32, "maxLength": 32}},
        },
    },
    {
        "name": "analyze_authorized_top30",
        "description": "Request the authorized Youpin CNY >100 GMV top-30 analysis. A tool error means authorization is unavailable; never substitute another leaderboard.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
]


def mcp_result(value, *, is_error=False):
    result = {"content": [{"type": "text", "text": __import__("json").dumps(value, ensure_ascii=False)}]}
    if is_error:
        result["isError"] = True
    return result


def mcp_response(request_id, *, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request_id}
    if error:
        response["error"] = error
    else:
        response["result"] = result
    return JSONResponse(response)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default response can echo inputs; never echo credentials or image data.
    return JSONResponse(
        {"detail": "invalid_request", "fields": [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]},
        status_code=422,
    )


@app.exception_handler(SourceError)
async def source_error(request, exc):
    status = 429 if exc.code in ("csqaq_rate_limited", "job_queue_full") else 503
    if exc.code in ("image_invalid", "image_format_or_dimensions_invalid"):
        status = 422
    if exc.code == "image_size_limit_8mb":
        status = 413
    return JSONResponse(
        {"detail": exc.code},
        status_code=status,
        headers={"Retry-After": str(exc.retry_after or 60)} if status == 429 else {},
    )


class ScoreRequest(BaseModel):
    liquidity: float = Field(ge=0, le=25)
    trend: float = Field(ge=0, le=20)
    valuation: float = Field(ge=0, le=20)
    profit_space: float = Field(ge=0, le=20)
    supply_demand: float = Field(ge=0, le=10)
    market_risk: float = Field(ge=0, le=5)


class UpstreamRequest(BaseModel):
    payload: dict = Field(default_factory=dict)


@app.get("/")
def index():
    return {"name": "CS2 Skin Analyzer", "version": "0.2.0", "docs": "/docs"}


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def status():
    return {
        "csqaq_configured": bool(os.getenv("CSQAQ_API_TOKEN")),
        "proxy_access_configured": bool(os.getenv("ANALYZER_ACCESS_KEY")),
    }


@app.post("/api/score")
def score(data: ScoreRequest):
    total = round(sum(data.model_dump().values()), 1)
    tier = "优先关注" if total >= 85 else "重点关注" if total >= 75 else "观察" if total >= 65 else "暂不买入"
    return {
        "score": total,
        "tier": tier,
        "components": data.model_dump(),
        "note": "人工输入的旧版评分，不代表实时行情或买入信号，不参与自动报告",
    }


@app.get("/api/capabilities")
def capabilities():
    return {
        "csqaq_token_configured": bool(os.getenv("CSQAQ_API_TOKEN")),
        "vision_configured": bool(os.getenv("OPENAI_API_KEY") and os.getenv("VISION_MODEL")),
        "youpin_direct": "authorization_required",
        "target_top30": "unverified_unavailable",
        "csqaq_yyyp_fields": "documented_not_live_verified",
        "youpin_volume_depth_fee": "unavailable",
        "chatgpt_connection": "requires_user_action_setup",
        "storage": "SQLite; /tmp is ephemeral unless ANALYZER_DB_PATH points to a persistent volume",
        "execution": "single worker, single replica; 60 authenticated requests/min; 8 pending jobs; 24h retention",
    }


@app.post("/mcp")
async def mcp(request: Request):
    try:
        message = await request.json()
    except ValueError:
        return mcp_response(None, error={"code": -32700, "message": "Invalid JSON"})
    if not isinstance(message, dict):
        return mcp_response(None, error={"code": -32600, "message": "Invalid request"})
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "server/discover":
        return mcp_response(request_id, result={"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}})
    if method == "tools/list":
        return mcp_response(request_id, result={"tools": MCP_TOOLS})
    if method != "tools/call" or not isinstance(params, dict):
        return mcp_response(request_id, error={"code": -32601, "message": "Method not found"})
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return mcp_response(request_id, result=mcp_result({"detail": "invalid_arguments"}, is_error=True))
    try:
        if name == "get_analyzer_capabilities":
            value = capabilities()
        elif name == "analyze_items":
            body = ItemsRequest.model_validate({**arguments, "origin": arguments.get("origin", "chatgpt_vision")})
            value = request.app.state.jobs.submit(request.state.owner, request=body)
        elif name == "get_analysis_job":
            job_id = arguments.get("job_id", "")
            value = request.app.state.jobs.get(job_id, request.state.owner)
            if not value:
                raise ValueError("job_not_found")
        elif name == "analyze_authorized_top30":
            await youpin_provider.top30()
            value = {"status": "completed"}
        else:
            raise ValueError("unknown_tool")
        return mcp_response(request_id, result=mcp_result(value))
    except (ValueError, SourceError) as exc:
        return mcp_response(request_id, result=mcp_result({"detail": str(exc)}, is_error=True))


# Existing endpoint paths retained; corrected supply to the documented GET method.
ALLOWED = {
    "rank": ("POST", "info/get_rank_list"),
    "supply": ("GET", "info/good/statistic"),
    "volume": ("POST", "info/vol_data_detail"),
}


@app.post("/api/csqaq/{kind}")
async def csqaq(kind: str, body: UpstreamRequest):
    if kind not in ALLOWED:
        raise HTTPException(404, "Unsupported endpoint")
    method, path = ALLOWED[kind]
    result = await csqaq_provider.request(method, path, body.payload)
    return {k: v for k, v in result.items() if k != "_queried_at"}


@app.get("/api/csqaq/diagnostic")
async def csqaq_diagnostic():
    try:
        r = await csqaq_provider.request("GET", "info/good", {"id": 7310})
        envelope = r.get("data")
        info = envelope.get("goods_info") if isinstance(envelope, dict) else None
        if not isinstance(info, dict) or info.get("id") != 7310:
            raise SourceError("csqaq_detail_schema_changed")
        return {
            "status": "validated_response",
            "queried_at": r["_queried_at"],
            "available_fields": [
                k
                for k in ("yyyp_sell_price", "yyyp_buy_price", "yyyp_sell_num", "yyyp_buy_num", "updated_at")
                if info.get(k) is not None
            ],
        }
    except SourceError as e:
        return {"status": e.code, "retry_after": e.retry_after}


@app.post("/api/analyze/items", status_code=202)
async def analyze_items(body: ItemsRequest, request: Request):
    return request.app.state.jobs.submit(request.state.owner, request=body)


async def image_body(request):
    raw = await request.body()
    validate_image(raw)
    return raw


@app.post("/api/recognize/image")
async def recognize_image(request: Request, expected_count: int = 30):
    if not 1 <= expected_count <= 30:
        raise HTTPException(422, "expected_count must be 1..30")
    result = await vision_provider.recognize(await image_body(request), expected_count)
    return result.model_dump(mode="json")


@app.post("/api/analyze/image", status_code=202)
async def analyze_image(request: Request, expected_count: int = 30):
    if not 1 <= expected_count <= 30:
        raise HTTPException(422, "expected_count must be 1..30")
    raw = await image_body(request)
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("VISION_MODEL"):
        raise SourceError("vision_not_configured")
    return request.app.state.jobs.submit(request.state.owner, raw=raw, expected_count=expected_count)


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(404, "Job not found")
    result = request.app.state.jobs.get(job_id, request.state.owner)
    if not result:
        raise HTTPException(404, "Job not found")
    return result


@app.get("/api/analyze/top30")
async def analyze_top30():
    await youpin_provider.top30()


@app.get("/api/actions-schema")
def actions_schema():
    import json
    from pathlib import Path

    return json.loads(Path("docs/actions-openapi.json").read_text())
