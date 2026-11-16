from .models import ItemsRequest, now
from .providers import SourceError
from .matching import exact_match
from .metrics import analyze


async def build_report(request: ItemsRequest, csqaq, youpin):
    rows = []
    abort_reason = None
    seen = set()
    for index, item in enumerate(request.items):
        row = {
            "row": index + 1,
            "input": item.model_dump(mode="json"),
            "match": None,
            "status": "待核实",
            "youpin": None,
            "csqaq": None,
            "analysis": None,
            "issues": [],
        }
        rows.append(row)
        if abort_reason:
            row["issues"].append(abort_reason + "；本批后续请求已停止")
            continue
        if item.needs_review or item.confidence < 0.95 or item.version == "unknown" or item.pattern:
            _, reason = exact_match(item, [])
            row["issues"].append(reason)
            continue
        try:
            candidates = await csqaq.search(item.name)
            identity, reason = exact_match(item, candidates)
            if identity is None:
                row["issues"].append(reason)
                continue
            row["match"] = {k: identity.get(k) for k in ("id", "name", "market_hash_name")}
            if str(identity["id"]) in seen:
                row["issues"].append("重复饰品行，保留识别结果但不重复评分或排名")
                continue
            seen.add(str(identity["id"]))
            direct = await youpin.snapshot(identity)
            snapshot, aux = await csqaq.snapshot(identity)
            snapshot.fields.update(direct.fields)
            snapshot.series.update(direct.series)
            snapshot.issues.extend(direct.issues)
            for error in snapshot.issues:
                if error.startswith(("csqaq_auth_or_ip_whitelist_rejected", "csqaq_rate_limited")):
                    abort_reason = error.split(":")[0]
            row.update(
                status="已获取部分行情"
                if snapshot.fields or any(s.points for s in snapshot.series.values())
                else "数据不足",
                youpin=snapshot.model_dump(mode="json"),
                csqaq=aux,
                analysis=analyze(snapshot),
            )
            if row["analysis"]["total"] is not None:
                row["status"] = "已完成评分"
        except SourceError as e:
            row["issues"].append(e.code)
            if e.code in (
                "csqaq_token_not_configured",
                "csqaq_auth_or_ip_whitelist_rejected",
                "csqaq_rate_limited",
                "csqaq_connection_error",
                "csqaq_timeout",
            ):
                abort_reason = e.code
    ranked = sorted(
        (r for r in rows if r["analysis"] and r["analysis"]["total"] is not None),
        key=lambda r: (-r["analysis"]["total"], r["row"]),
    )
    ranking = [
        {
            "rank": i + 1,
            "row": r["row"],
            "name": r["match"]["name"],
            "total": r["analysis"]["total"],
            "basis": r["analysis"]["components"],
        }
        for i, r in enumerate(ranked)
    ]
    acquired = sum(
        bool(r["youpin"] and (r["youpin"]["fields"] or any(s["points"] for s in r["youpin"]["series"].values())))
        for r in rows
    )
    return {
        "generated_at": now().isoformat(),
        "origin": request.origin,
        "expected_count": request.expected_count,
        "recognized_count": len(rows),
        "matched_count": sum(r["match"] is not None for r in rows),
        "market_data_count": acquired,
        "scored_count": len(ranked),
        "missing_rows": max(0, request.expected_count - len(rows)),
        "completion": "完整评分" if len(ranked) == request.expected_count else "部分完成 / 数据不足",
        "items": rows,
        "ranking": ranking,
        "research_focus": ranking[:5],
        "notes": [
            "仅可评分饰品进入排名和重点研究清单；数据不足不生成推荐。",
            "截图价格仅为识别原文，不作为实时行情；origin不证明榜单来源真实性。",
            "objective-v1为固定描述性评分，未经过收益回测，不代表上涨概率。",
        ],
    }
