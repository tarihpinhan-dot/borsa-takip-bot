import pandas as pd
import requests
from io import StringIO


def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    tables = pd.read_html(StringIO(response.text))
    df = tables[0]

    tickers = df["Symbol"].tolist()
    tickers = [t.replace(".", "-") for t in tickers]

    return tickers
