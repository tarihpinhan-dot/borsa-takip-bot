import os
import yfinance as yf
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

stocks = [
    "AAPL","MSFT","TSLA","NVDA","AMZN","META","GOOGL","AMD","NFLX",
    "PLTR","SOFI","BABA","TSM","SPY","QQQ","DIS","UBER","COIN","MARA","RIOT"
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
        if change > 2: score += 2
        if change > 4: score += 3
        if change > 6: score += 4

        # trend
        if trend > 5: score += 3

        # volatilite (hareket var ama aşırı değil)
        if 0.8 < vol < 5:
            score += 2

        # filtre: çok zayıfları ele
        if score >= 5:
            results.append((s, change, trend, score))

    except:
        continue

results.sort(key=lambda x: x[3], reverse=True)

if results:
    msg = "🚀 AI MARKET RADAR (GÜNLÜK)\n\n"

    for r in results[:3]:
        symbol = r[0]
        change = r[1]
        trend = r[2]
        score = r[3]

        # basit yorum motoru
        if score >= 8:
            label = "🔥 AL (güçlü momentum)"
        elif score >= 6:
            label = "👀 İZLE"
        else:
            label = "⚠️ ZAYIF"

        msg += f"{symbol}\n"
        msg += f"1D: {change:.2f}% | 5D: {trend:.2f}%\n"
        msg += f"Score: {score} → {label}\n\n"

    msg += "🧠 AI: Momentum + trend + volatilite analiz edildi\n"
    msg += "⚠️ Yatırım tavsiyesi değildir"

    send(msg)
else:
    send("Bugün güçlü sinyal yok.")
