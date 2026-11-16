"""Run manually in an authorized environment. Prints field names, never tokens or raw messages."""

import asyncio
import json

from analyzer.providers import CSQAQ, SourceError, Youpin


async def main():
    result = {}
    try:
        r = await CSQAQ().request("GET", "info/good", {"id": 7310})
        envelope = r.get("data")
        fields = envelope.get("goods_info") if isinstance(envelope, dict) else None
        if not isinstance(fields, dict) or fields.get("id") != 7310:
            raise SourceError("csqaq_detail_schema_changed")
        result["csqaq"] = {
            "status": "live_verified",
            "queried_at": r["_queried_at"],
            "fields": [
                k
                for k in (
                    "yyyp_sell_price",
                    "yyyp_buy_price",
                    "yyyp_sell_num",
                    "yyyp_buy_num",
                    "statistic",
                    "updated_at",
                )
                if fields.get(k) is not None
            ],
        }
    except SourceError as exc:
        result["csqaq"] = {"status": exc.code}
    try:
        await Youpin().top30()
    except SourceError as exc:
        result["target_top30"] = {"status": exc.code}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
