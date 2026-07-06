def calculate_metrics(hist):
    if hist is None or len(hist) < 5:
        return None

    close = hist["Close"]
    volume = hist["Volume"]

    last_close = close.iloc[-1]
    prev_close = close.iloc[-2]

    daily_change = ((last_close - prev_close) / prev_close) * 100
    trend_5d = ((last_close - close.iloc[0]) / close.iloc[0]) * 100
    volatility = close.pct_change().std() * 100

    avg_volume = volume.mean()
    last_volume = volume.iloc[-1]

    volume_ratio = last_volume / avg_volume if avg_volume > 0 else 0

    return {
        "price": last_close,
        "daily_change": daily_change,
        "trend_5d": trend_5d,
        "volatility": volatility,
        "volume_ratio": volume_ratio
    }
