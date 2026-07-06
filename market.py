import pandas as pd


def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    tables = pd.read_html(url)

    df = tables[0]
    tickers = df["Symbol"].tolist()

    # Yahoo/Finnhub uyumu için noktalı sembolleri düzelt
    tickers = [t.replace(".", "-") for t in tickers]

    return tickers
