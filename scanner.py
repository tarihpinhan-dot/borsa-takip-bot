import yfinance as yf

from config import MIN_SCORE, TOP_N
from indicators import calculate_metrics
from market import get_sp500_tickers
from scoring import calculate_score


def scan_market():
    tickers = get_sp500_tickers()
    results = []

    for symbol in tickers:
        try:
            hist = yf.Ticker(symbol).history(period="7d")

            metrics = calculate_metrics(hist)
            if not metrics:
                continue

            score = calculate_score(metrics)
            if score is None or score < MIN_SCORE:
                continue

            results.append({
                "symbol": symbol,
                "score": score,
                "price": metrics["price"],
                "daily_change": metrics["daily_change"],
                "trend_5d": metrics["trend_5d"],
                "volatility": metrics["volatility"],
                "volume_ratio": metrics["volume_ratio"],
            })

        except Exception as e:
            print(f"{symbol} hata: {e}")
            continue

    results.sort(key=lambda x: x["score"], reverse=True)

    return results[:TOP_N]
