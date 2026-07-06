import os
import yfinance as yf
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# Daha geniş tarama listesi
stocks = [
    "AAPL","MSFT","TSLA","NVDA","AMZN","META","GOOGL","AMD","NFLX",
    "INTC","PLTR","SOFI","BABA","TSM","SPY","QQQ","ARKK","DIS","UBER",
    "SHOP","COIN","MARA","RIOT","LCID","NIO"
]

results = []

for s in stocks:
    try:
        t = yf.Ticker(s)
        hist = t.history(period="5d")

        if len(hist) < 5:
            continue

        # günlük değişim
        change = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100

        # 5 günlük trend
        trend = ((hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0]) * 100

        # volatilite
        vol = hist["Close"].pct_change().std() * 100

        score = 0

        # momentum
        if change > 2:
            score += 2
        if change > 4:
            score += 3
        if change > 6:
            score += 4

        # trend
        if trend > 5:
            score += 3

        # volatilite (hareket var mı)
        if vol > 1:
            score += 2

        if score >= 4:
            results.append((s, change, trend, score))

    except:
        continue

# sırala
results.sort(key=lambda x: x[3], reverse=True)

# TOP 3
if results:
    msg = "🚀 DAILY AI MARKET RADAR\n\n"

    for r in results[:3]:
        msg += f"{r[0]}\n1D: {r[1]:.2f}% | 5D: {r[2]:.2f}%\nScore: {r[3]}\n\n"

    msg += "🧠 AI: Güçlü momentum ve trend gösteren hisseler\n"
    msg += "⚠️ Bu bir yatırım tavsiyesi değildir"

    send(msg)
else:
    send("Bugün güçlü sinyal yok.")
