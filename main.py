import os
import yfinance as yf
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text})

# Takip edilecek bazı büyük hisseler (başlangıç listesi)
stocks = ["AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "META", "GOOGL"]

messages = []

for symbol in stocks:
    data = yf.Ticker(symbol)
    hist = data.history(period="2d")

    if len(hist) < 2:
        continue

    yesterday = hist.iloc[-2]
    today = hist.iloc[-1]

    change = ((today["Close"] - yesterday["Close"]) / yesterday["Close"]) * 100

    if change >= 2.5:  # momentum filtresi
        msg = f"""🚀 {symbol}
Günlük değişim: {change:.2f}%

📊 Momentum güçlü
🧠 AI yorum: İlgi artışı olabilir
"""
        messages.append(msg)

if messages:
    send_message("\n\n".join(messages))
else:
    send_message("Bugün güçlü momentum yakalanan hisse yok.")
