import os
import requests


FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
BASE_URL = "https://finnhub.io/api/v1"


def finnhub_get(endpoint, params=None):
    if not FINNHUB_API_KEY:
        raise ValueError("FINNHUB_API_KEY eksik. GitHub Secrets kontrol edilmeli.")

    if params is None:
        params = {}

    params["token"] = FINNHUB_API_KEY

    response = requests.get(
        f"{BASE_URL}/{endpoint}",
        params=params,
        timeout=20
    )

    response.raise_for_status()
    return response.json()


def get_quote(symbol):
    return finnhub_get("quote", {"symbol": symbol})


def get_company_news(symbol, from_date, to_date):
    return finnhub_get(
        "company-news",
        {
            "symbol": symbol,
            "from": from_date,
            "to": to_date
        }
    )
