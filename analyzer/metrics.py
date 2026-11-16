"""Deterministic score v1. Missing dimensions never get imputed or reweighted."""

import math
import statistics
from datetime import timedelta, timezone
from .models import Snapshot, now

WEIGHTS = {"liquidity": 25, "profit_space": 25, "trend": 20, "valuation": 15, "supply_demand": 10, "market_risk": 5}


def clip(x):
    return min(1.0, max(0.0, x))


def analyze(s: Snapshot, at=None) -> dict:
    at = at or now()
    issues = list(s.issues)
    missing = []
    metrics = {}
    components = {k: None for k in WEIGHTS}
    evidence = {}

    def get(k):
        obs = s.fields.get(k)
        if obs is None:
            missing.append(k)
            return None
        if not math.isfinite(obs.value) or obs.value < 0:
            issues.append(f"{k}: 非法数值")
            return None
        if obs.observed_at is None or obs.observed_at.utcoffset() is None:
            issues.append(f"{k}: 来源时间或时区未知，不能用于评分")
            return None
        age = (at - obs.observed_at).total_seconds()
        max_age = 30 * 86400 if k == "sell_fee_rate" else 3600
        if age < -300 or age > max_age:
            issues.append(f"{k}: 数据过期或时间异常")
            return None
        return obs.value

    def daily(k, days=30):
        series = s.series.get(k)
        if not series:
            missing.append(k)
            return None
        ordered = sorted(series.points, key=lambda p: p.at)
        # Epochs are UTC; one last sample per UTC date. No interpolation.
        buckets = {}
        for p in ordered:
            if p.at.utcoffset() is None or p.at > at + timedelta(minutes=5):
                issues.append(f"{k}: 时间异常")
                return None
            if math.isfinite(p.value):
                buckets[p.at.astimezone(timezone.utc).date()] = p.value
        end = at.astimezone(timezone.utc).date() - timedelta(days=1)
        dates = [end - timedelta(days=days - i - 1) for i in range(days)]
        if any(d not in buckets for d in dates):
            issues.append(f"{k}: 缺少连续{days}个已完成UTC日样本")
            return None
        return [buckets[d] for d in dates]

    ask, bid = get("ask"), get("bid")
    fee = get("sell_fee_rate")
    sell, buy = get("sell_count"), get("buy_count")
    v1, v7, v30 = get("volume_24h"), get("volume_7d"), get("volume_30d")
    prices = daily("price_history")
    sell_history = daily("sell_history", 8)
    buy_history = daily("buy_history", 8)
    supply_history = daily("supply_history", 8)
    depth = get("bid_depth_1pct")
    book_ok = ask is not None and ask > 0 and bid is not None and bid > 0 and bid <= ask
    if ask is not None and bid is not None and not book_ok:
        issues.append("买卖报价无效或倒挂，相关指标不计算")
    if fee is not None and fee >= 1:
        fee = None
        issues.append("手续费率必须小于1")
    if book_ok:
        spread = ask - bid
        metrics.update(spread_cny=round(spread, 6), spread_pct=100 * spread / ask)
        if fee is not None:
            net = bid * (1 - fee) - ask
            metrics.update(immediate_pnl_cny=round(net, 6), immediate_pnl_pct=100 * net / ask)
            components["profit_space"] = 25 * clip(1 + (net / ask) / 0.10)
            evidence["profit_space"] = "25×clip(1+即时净损益率/0.10); 0%损益为25分，-10%为0分"
    if v1 is not None and v7 is not None and v30 is not None:
        if v30 < v7 or v7 < v1:
            issues.append("嵌套成交量窗口不一致")
        else:
            if v7 > 0:
                metrics["volume_24h_vs_7d_daily_pct"] = 100 * (v1 / (v7 / 7) - 1)
            if v30 > 0:
                metrics["volume_7d_vs_30d_daily_pct"] = 100 * ((v7 / 7) / (v30 / 30) - 1)
            if sell is not None and sell > 0 and depth is not None:
                turnover = clip((v7 / 7) / sell)
                executable = clip(depth / max(v1, 1))
                components["liquidity"] = 25 * (0.7 * turnover + 0.3 * executable)
                evidence["liquidity"] = "25×(0.7×clip(日均7日销量/在售数)+0.3×clip(1%内求购深度/max(24h销量,1)))"
    if prices and min(prices) > 0 and ask is not None and ask > 0:
        returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
        volatility = statistics.stdev(returns) if len(returns) > 1 else 0
        # Historical points are asks: use ask for comparable price trends.
        r7, r30 = ask / prices[-7] - 1, ask / prices[0] - 1
        metrics.update(
            price_change_7d_pct=100 * r7,
            price_change_30d_pct=100 * r30,
            daily_log_volatility_pct=100 * volatility,
            max_daily_log_move_pct=100 * max(map(abs, returns)),
        )
        components["trend"] = 20 * clip(0.5 + 0.5 * (0.4 * r7 + 0.6 * r30) / 0.20)
        evidence["trend"] = "20×clip(0.5+0.5×(0.4×7日收益率+0.6×30日收益率)/0.20)"
        lo, hi = min(prices), max(prices)
        if hi > lo:
            position = clip((ask - lo) / (hi - lo))
            metrics["historical_position_30d_pct"] = 100 * position
            components["valuation"] = 15 * (1 - position)
            evidence["valuation"] = "15×(1-clip((当前价-历史最低)/(历史最高-历史最低))); 平坦区间不评分"
        else:
            issues.append("历史价格区间为零，估值位置无法定义")
        components["market_risk"] = 5 * (1 - clip(volatility / 0.10))
        evidence["market_risk"] = "5×(1-clip(日对数收益率样本标准差/0.10))"
    if sell is not None and buy is not None:
        if sell > 0:
            metrics["buy_sell_count_ratio"] = buy / sell
        if all(x is not None and x[0] > 0 for x in [sell_history, buy_history, supply_history]):
            ds = sell_history[-1] / sell_history[0] - 1
            db = buy_history[-1] / buy_history[0] - 1
            dc = supply_history[-1] / supply_history[0] - 1
            metrics.update(
                sell_count_change_7d_pct=100 * ds, buy_count_change_7d_pct=100 * db, supply_change_7d_pct=100 * dc
            )
            components["supply_demand"] = 10 * clip(0.5 + 0.5 * (db - ds - dc))
            evidence["supply_demand"] = "10×clip(0.5+0.5×(求购数7日变化率-在售数7日变化率-存世量7日变化率))"
    if metrics.get("max_daily_log_move_pct", 0) > 20:
        issues.append("历史单日对数价格变化超过20%")
    if metrics.get("volume_24h_vs_7d_daily_pct", 0) > 200:
        issues.append("24h销量超过7日日均的3倍")
    total = round(sum(components.values()), 2) if all(v is not None for v in components.values()) else None
    return {
        "formula_version": "objective-v1",
        "status": "可评分" if total is not None else "数据不足",
        "total": total,
        "components": {k: round(v, 4) if v is not None else None for k, v in components.items()},
        "weights": WEIGHTS,
        "evidence": evidence,
        "metrics": metrics,
        "missing": sorted(set(missing)),
        "issues": sorted(set(issues)),
        "unscored_dimensions": [k for k, v in components.items() if v is None],
    }
