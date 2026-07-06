from config import (
    MIN_PRICE,
    MAX_PRICE,
    MIN_DAILY_CHANGE,
    MAX_DAILY_CHANGE,
    MIN_5D_TREND,
    MIN_VOLATILITY,
    MAX_VOLATILITY,
)


def calculate_score(metrics):
    price = metrics["price"]
    daily_change = metrics["daily_change"]
    trend_5d = metrics["trend_5d"]
    volatility = metrics["volatility"]
    volume_ratio = metrics["volume_ratio"]

    if price < MIN_PRICE or price > MAX_PRICE:
        return None

    if daily_change < MIN_DAILY_CHANGE or daily_change > MAX_DAILY_CHANGE:
        return None

    if trend_5d < MIN_5D_TREND:
        return None

    if volatility < MIN_VOLATILITY or volatility > MAX_VOLATILITY:
        return None

    score = 0

    score += min(daily_change * 4, 30)
    score += min(max(trend_5d, 0) * 3, 25)
    score += min(volume_ratio * 15, 25)
    score += min(volatility * 2, 20)

    return round(score, 1)
