import math
from datetime import datetime, timedelta, timezone

import pytest

from analyzer.matching import exact_match
from analyzer.metrics import analyze
from analyzer.models import Item, Observation, Point, Series, Snapshot

AT = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def snapshot():
    s = Snapshot()
    values = {
        "ask": 100,
        "bid": 98,
        "sell_fee_rate": 0.01,
        "sell_count": 100,
        "buy_count": 100,
        "volume_24h": 10,
        "volume_7d": 70,
        "volume_30d": 300,
        "bid_depth_1pct": 10,
    }
    for key, value in values.items():
        s.fields[key] = Observation(
            value=value,
            unit="test",
            source="synthetic-test-only",
            endpoint="fixture",
            field=key,
            queried_at=AT,
            observed_at=AT,
            basis="synthetic fixture, never a live quote",
        )
    for key in ["price_history", "sell_history", "buy_history", "supply_history"]:
        points = [
            Point(at=AT - timedelta(days=30 - i), value=70 + i if key == "price_history" else 100) for i in range(30)
        ]
        s.series[key] = Series(
            points=points, unit="test", source="synthetic-test-only", endpoint="fixture", queried_at=AT, basis="fixture"
        )
    return s


def test_hand_calculated_metrics_and_score():
    result = analyze(snapshot(), AT)
    assert result["metrics"]["spread_cny"] == 2
    assert result["metrics"]["spread_pct"] == 2
    assert result["metrics"]["immediate_pnl_cny"] == -2.98
    assert result["components"]["profit_space"] == pytest.approx(17.55)
    assert result["components"]["liquidity"] == pytest.approx(9.25)
    assert result["components"]["supply_demand"] == 5
    assert result["components"]["valuation"] == 0
    assert result["total"] is not None
    assert 0 <= result["total"] <= 100


def test_missing_data_never_gets_total():
    s = snapshot()
    del s.fields["volume_24h"]
    result = analyze(s, AT)
    assert result["total"] is None
    assert "liquidity" in result["unscored_dimensions"]
    assert result["components"]["profit_space"] is not None


@pytest.mark.parametrize("change", ["crossed", "zero", "stale", "timezone", "nan", "missing_fee", "negative"])
def test_quote_validation(change):
    s = snapshot()
    if change == "crossed":
        s.fields["bid"].value = 101
    if change == "zero":
        s.fields["ask"].value = 0
    if change == "stale":
        s.fields["ask"].observed_at = AT - timedelta(days=2)
    if change == "timezone":
        s.fields["ask"].observed_at = None
    if change == "nan":
        s.fields["ask"].value = math.nan
    if change == "negative":
        s.fields["ask"].value = -1
    if change == "missing_fee":
        del s.fields["sell_fee_rate"]
    r = analyze(s, AT)
    assert r["total"] is None
    assert r["components"]["profit_space"] is None


def test_history_gaps_and_flat_range():
    s = snapshot()
    s.series["price_history"].points.pop(5)
    assert analyze(s, AT)["components"]["trend"] is None
    s = snapshot()
    for p in s.series["price_history"].points:
        p.value = 100
    r = analyze(s, AT)
    assert r["components"]["valuation"] is None
    assert r["total"] is None


def test_nested_volume_invalid():
    s = snapshot()
    s.fields["volume_24h"].value = 1000
    r = analyze(s, AT)
    assert r["components"]["liquidity"] is None
    assert any("成交量窗口" in i for i in r["issues"])


NORMAL = {"id": 1, "name": "AK-47 | 红线 (久经沙场)", "market_hash_name": "AK-47 | Redline (Field-Tested)"}
ST = {
    "id": 2,
    "name": "StatTrak™ AK-47 | 红线 (久经沙场)",
    "market_hash_name": "StatTrak™ AK-47 | Redline (Field-Tested)",
}


def test_exact_identity_normalization():
    i = Item(name="AK-47 | 红线（久经沙场）", version="normal", wear="久经沙场")
    assert exact_match(i, [NORMAL, ST])[0] == NORMAL


@pytest.mark.parametrize(
    "item",
    [
        Item(name="AK-47 | 红线", version="normal"),
        Item(name=NORMAL["name"], version="stattrak"),
        Item(name=NORMAL["name"], version="unknown"),
        Item(name=NORMAL["name"], version="normal", pattern="661"),
        Item(name=NORMAL["name"], version="normal", wear="崭新出厂"),
        Item(name=NORMAL["name"], version="normal", confidence=0.8),
        Item(name=NORMAL["name"], version="normal", market_hash_name=ST["market_hash_name"]),
    ],
)
def test_uncertain_or_different_variants_never_match(item):
    assert exact_match(item, [NORMAL, ST])[0] is None


def test_sticker_finish_and_souvenir_distinct():
    c = {"id": 3, "name": "印花 | 测试 (全息)", "market_hash_name": "Sticker | Test (Holo)"}
    assert exact_match(Item(name="印花 | 测试 (闪亮)", version="normal"), [c])[0] is None
    assert exact_match(Item(name=NORMAL["name"], version="souvenir"), [NORMAL])[0] is None


def test_trend_remains_available_without_bid():
    s = snapshot()
    del s.fields["bid"]
    r = analyze(s, AT)
    assert r["components"]["trend"] is not None
    assert r["components"]["profit_space"] is None
    assert r["total"] is None


def test_history_bucket_is_utc_even_for_offset_timestamps():
    s = snapshot()
    expected = analyze(s, AT)
    for series in s.series.values():
        for point in series.points:
            point.at = point.at.astimezone(timezone(timedelta(hours=14)))
    assert analyze(s, AT) == expected
