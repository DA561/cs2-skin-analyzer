"""Exact identity matching. Never discard version, finish or pattern tokens."""

import re
import unicodedata
from .models import Item


def normalize(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()


def version_of(name: str) -> str:
    text = normalize(name)
    if "stattrak" in text or "暗金" in text:
        return "stattrak"
    if "souvenir" in text or "纪念品" in text:
        return "souvenir"
    return "normal"


def exact_match(item: Item, candidates: list[dict]) -> tuple[dict | None, str]:
    if item.needs_review or item.confidence < 0.95 or item.version == "unknown":
        return None, "识别或版本不确定，待核实"
    if item.pattern:
        return None, "特殊模板/图案行情尚无已验证的悠悠有品接入，待核实"
    matches = []
    for c in candidates:
        if normalize(item.name) not in {normalize(c.get("name", "")), normalize(c.get("market_hash_name", ""))}:
            continue
        if item.market_hash_name and normalize(item.market_hash_name) != normalize(c.get("market_hash_name", "")):
            continue
        identity = c.get("market_hash_name") or c.get("name", "")
        if version_of(identity) != item.version:
            continue
        if item.wear:
            wear_pairs = {
                "崭新出厂": "Factory New",
                "略有磨损": "Minimal Wear",
                "久经沙场": "Field-Tested",
                "破损不堪": "Well-Worn",
                "战痕累累": "Battle-Scarred",
            }
            acceptable = {normalize(item.wear), normalize(wear_pairs.get(item.wear, item.wear))}
            names = normalize(c.get("name", "") + c.get("market_hash_name", ""))
            if not any(w in names for w in acceptable):
                continue
        matches.append(c)
    unique = {str(c["id"]): c for c in matches if "id" in c}
    if len(unique) != 1:
        return None, "完整名称/磨损/版本无法唯一精确匹配，待核实"
    return next(iter(unique.values())), "完整名称、版本及附加身份字段精确匹配"
