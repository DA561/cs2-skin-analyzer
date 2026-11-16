"""Documented CSQAQ endpoints. No scraping or authentication bypass."""

import asyncio
import copy
import math
import os
import time
from datetime import datetime

import httpx

from .models import Observation, Point, Series, Snapshot, now

BASE_URL = "https://api.csqaq.com/api/v1"
MAX_RESPONSE = 4 * 1024 * 1024


class SourceError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


class CSQAQ:
    def __init__(self, client=None, interval=1.1):
        self.client = client
        self.interval = max(1.1, interval)
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.cooldown = 0.0
        self.cache = {}
        self.cache_bytes = 0

    async def request(self, method, path, payload=None):
        token = os.getenv("CSQAQ_API_TOKEN")
        if not token:
            raise SourceError("csqaq_token_not_configured")
        key = (method, path, repr(payload))
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return copy.deepcopy(cached[1])
        async with self.lock:
            wait = max(self.last + self.interval, self.cooldown) - time.monotonic()
            if wait > 60:
                raise SourceError("csqaq_rate_limited", math.ceil(wait))
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.monotonic()
            client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=False)
            try:
                kwargs = {"params": payload} if method == "GET" else {"json": payload}
                async with client.stream(
                    method, BASE_URL + "/" + path, headers={"ApiToken": token}, **kwargs
                ) as response:
                    status = response.status_code
                    if status == 429:
                        try:
                            delay = max(1, min(3600, int(response.headers.get("Retry-After", "60"))))
                        except ValueError:
                            delay = 60
                        self.cooldown = time.monotonic() + delay
                        raise SourceError("csqaq_rate_limited", delay)
                    if status in (400, 401, 403):
                        raise SourceError("csqaq_auth_or_ip_whitelist_rejected")
                    if status >= 400 or status < 200 or status >= 300:
                        raise SourceError("csqaq_http_error")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE:
                            raise SourceError("csqaq_response_too_large")
                import json

                try:
                    result = json.loads(raw)
                except ValueError:
                    raise SourceError("csqaq_invalid_json") from None
                if not isinstance(result, dict):
                    raise SourceError("csqaq_invalid_schema")
                code = result.get("code")
                if code not in (0, 200):
                    if code in (400, 401, 403):
                        raise SourceError("csqaq_auth_or_ip_whitelist_rejected")
                    if code == 429:
                        self.cooldown = time.monotonic() + 60
                        raise SourceError("csqaq_rate_limited", 60)
                    raise SourceError("csqaq_business_error")
                result["_queried_at"] = now().isoformat()
                if len(raw) <= 256 * 1024:
                    if key in self.cache:
                        self.cache_bytes -= self.cache.pop(key)[2]
                    while self.cache and (len(self.cache) >= 128 or self.cache_bytes + len(raw) > 8 * 1024 * 1024):
                        self.cache_bytes -= self.cache.pop(next(iter(self.cache)))[2]
                    self.cache[key] = (time.monotonic(), copy.deepcopy(result), len(raw))
                    self.cache_bytes += len(raw)
                return result
            except httpx.TimeoutException:
                raise SourceError("csqaq_timeout") from None
            except httpx.HTTPError:
                raise SourceError("csqaq_connection_error") from None
            finally:
                if self.client is None:
                    await client.aclose()

    async def search(self, text):
        r = await self.request("POST", "info/get_good_id", {"page_index": 1, "page_size": 100, "search": text})
        envelope = r.get("data")
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("name"), str)
            or not isinstance(c.get("market_hash_name"), str)
            or not str(c.get("id", "")).isdigit()
            for c in data.values()
        ):
            raise SourceError("csqaq_search_schema_changed")
        # Only exact matches are used; a truncated search cannot select a near match.
        return list(data.values())

    async def snapshot(self, identity):
        good_id = int(identity["id"])
        result = await self.request("GET", "info/good", {"id": good_id})
        envelope = result.get("data")
        info = envelope.get("goods_info") if isinstance(envelope, dict) else None
        if not isinstance(info, dict) or any(not isinstance(info.get(k), str) for k in ("name", "market_hash_name")):
            raise SourceError("csqaq_detail_schema_changed")
        from .matching import normalize

        if str(info.get("id")) != str(good_id) or any(
            normalize(info.get(k, "")) != normalize(identity.get(k, "")) for k in ("name", "market_hash_name")
        ):
            raise SourceError("csqaq_identity_changed")
        queried = datetime.fromisoformat(result["_queried_at"])
        observed = parse_time(info.get("updated_at"))
        snapshot = Snapshot()
        if observed is None:
            snapshot.issues.append("CSQAQ updated_at未提供可验证时区或格式，保留原文但不用于评分")
        mapping = {
            "ask": ("yyyp_sell_price", "CNY", "悠悠有品最低在售价，由CSQAQ采集"),
            "bid": ("yyyp_buy_price", "CNY", "悠悠有品最高求购价，由CSQAQ采集；非成交保证"),
            "sell_count": ("yyyp_sell_num", "件", "CSQAQ口径：平台该指标最大值，不等于逐价位深度"),
            "buy_count": ("yyyp_buy_num", "件", "CSQAQ口径：平台该指标最大值，不等于逐价位深度"),
            "supply": ("statistic", "件", "CSQAQ记录的存世量，非保证覆盖全部库存"),
        }
        for key, (field, unit, basis) in mapping.items():
            value = number(info.get(field))
            if value is not None and not (key in ("ask", "bid") and value == 0):
                snapshot.fields[key] = Observation(
                    value=value,
                    unit=unit,
                    source="CSQAQ / 悠悠有品" if key != "supply" else "CSQAQ",
                    endpoint=BASE_URL + "/info/good",
                    field=field,
                    queried_at=queried,
                    observed_at=observed,
                    basis=basis,
                )
            elif info.get(field) is not None:
                snapshot.issues.append(f"{field}: 无效或零占位值")
        for key, upstream_key, unit in [
            ("price_history", "sell_price", "CNY"),
            ("sell_history", "sell_num", "件"),
            ("buy_history", "buy_num", "件"),
        ]:
            try:
                chart = await self.request(
                    "POST",
                    "info/chart",
                    {"good_id": good_id, "key": upstream_key, "platform": 2, "period": 30, "style": "all_style"},
                )
                data = chart.get("data")
                if not isinstance(data, dict):
                    raise SourceError("csqaq_chart_schema_changed")
                times, values = data.get("timestamp"), data.get("main_data")
                if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
                    raise SourceError("csqaq_chart_unaligned")
                points = []
                for timestamp, value in zip(times, values):
                    val = number(value)
                    if val is None or (key == "price_history" and val == 0):
                        continue
                    try:
                        stamp = float(timestamp)
                        if not math.isfinite(stamp) or stamp < 1e12:
                            raise ValueError()
                        dt = datetime.fromtimestamp(stamp / 1000, tz=now().tzinfo)
                    except (TypeError, ValueError, OverflowError, OSError):
                        raise SourceError("csqaq_chart_timestamp_invalid") from None
                    points.append(Point(at=dt, value=val))
                if len({p.at for p in points}) != len(points):
                    raise SourceError("csqaq_chart_duplicate_timestamps")
                snapshot.series[key] = Series(
                    points=points,
                    unit=unit,
                    source="CSQAQ / 悠悠有品",
                    endpoint=BASE_URL + "/info/chart",
                    queried_at=datetime.fromisoformat(chart["_queried_at"]),
                    basis=f"platform=2; key={upstream_key}; period=30; UTC日末采样，不插值",
                )
            except SourceError as e:
                snapshot.issues.append(e.code + ": " + key)
                if e.code in ("csqaq_auth_or_ip_whitelist_rejected", "csqaq_rate_limited"):
                    break
        snapshot.issues.extend(
            [
                "悠悠有品实际成交价、24h/7d/30d成交量、成交额、逐档深度尚无已验证数据源",
                "turnover_number/turnover_avg_price未映射为悠悠有品数据；文档日成交量仅支持Steam",
                "平台实际手续费未核验，净损益及总分可能为空",
            ]
        )
        try:
            if any(
                i.startswith(("csqaq_auth_or_ip_whitelist_rejected", "csqaq_rate_limited")) for i in snapshot.issues
            ):
                raise SourceError("csqaq_supply_skipped_after_provider_rejection")
            history = await self.request("GET", "info/good/statistic", {"id": good_id})
            records = history.get("data")
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise SourceError("csqaq_supply_schema_changed")
            points = []
            for record in records:
                dt = parse_time(record.get("created_at"))
                value = number(record.get("statistic"))
                if dt is None or value is None:
                    raise SourceError("csqaq_supply_timestamp_or_value_unverified")
                points.append(Point(at=dt, value=value))
            if len({p.at for p in points}) != len(points):
                raise SourceError("csqaq_supply_duplicate_timestamps")
            snapshot.series["supply_history"] = Series(
                points=points,
                unit="件",
                source="CSQAQ",
                endpoint=BASE_URL + "/info/good/statistic",
                queried_at=datetime.fromisoformat(history["_queried_at"]),
                basis="CSQAQ记录的存世量；来源时间必须带时区；日末UTC采样，不插值",
            )
        except SourceError as e:
            snapshot.issues.append(e.code + ": supply_history")
        # Explicitly whitelisted auxiliary fields; no raw response or upstream messages.
        auxiliary = {
            "identity": {k: info.get(k) for k in ("id", "yyyp_id", "name", "market_hash_name")},
            "source": "CSQAQ",
            "endpoint": BASE_URL + "/info/good",
            "queried_at": queried.isoformat(),
            "source_updated_at_raw": info.get("updated_at"),
            "fields": {"supply": snapshot.fields["supply"].model_dump(mode="json")}
            if "supply" in snapshot.fields
            else {},
        }
        return snapshot, auxiliary


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.utcoffset() is not None else None
    except ValueError:
        return None


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        val = float(value)
        return val if math.isfinite(val) and val >= 0 else None
    except (TypeError, ValueError):
        return None


class Youpin:
    """Fail closed until an officially authorized contract is verified."""

    async def top30(self):
        raise SourceError("youpin_today_gmv_top30_authorization_required")

    async def snapshot(self, identity):
        return Snapshot(issues=["悠悠有品官方授权适配器未接通；以下行情使用CSQAQ提供的悠悠有品字段"])
