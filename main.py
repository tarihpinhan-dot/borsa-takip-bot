import os
import yfinance as yf
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text})

stocks = ["AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "META", "GOOGL"]

results = []

for symbol in stocks:
    data = yf.Ticker(symbol)
    hist = data.history(period="2d")

    if len(hist) < 2:
        continue

    change = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100

    if change > 0:
        score = 0

        # momentum puanı
        if change >= 3:
            score += 2
        if change >= 5:
            score += 2
        if change >= 7:
            score += 2

        results.append((symbol, change, score))

# en güçlüleri sırala
results.sort(key=lambda x: x[2], reverse=True)

if results:
    msg = "🚀 BUGÜN EN GÜÇLÜ HİSSELER\n\n"

    for r in results[:5]:
        msg += f"{r[0]} | +{r[1]:.2f}% | Skor: {r[2]}\n"

    send_message(msg)
else:
    send_message("Bugün güçlü momentum yok.")
