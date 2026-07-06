import os
import yfinance as yf
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

stocks = [
    "AAPL","MSFT","TSLA","NVDA","AMZN","META","GOOGL",
    "AMD","NFLX","PLTR","SOFI","TSM","SPY","QQQ","COIN","DIS"
]

results = []

for s in stocks:
    try:
        t = yf.Ticker(s)
        hist = t.history(period="7d")

        if len(hist) < 5:
            continue

        change = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100
        trend = ((hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0]) * 100
        vol = hist["Close"].pct_change().std() * 100

        score = 0

        if change > 2: score += 2
        if change > 4: score += 3
        if change > 6: score += 4

        if trend > 5: score += 3

        if 0.8 < vol < 5:
            score += 2

        if score >= 5:
            results.append((s, change, trend, score))

    except:
        continue

results.sort(key=lambda x: x[3], reverse=True)

def ai_comment(score, change, trend):
    if score >= 8:
        return "Güçlü momentum + trend uyumu var. Alım ilgisi artmış olabilir."
    elif score >= 6:
        return "Orta güçlü hareket. Takip edilmeli."
    else:
        return "Zayıf sinyal, volatilite kaynaklı olabilir."

if results:
    msg = "🚀 AI TRADING RADAR\n\n"

    for r in results[:3]:
        s, change, trend, score = r
        comment = ai_comment(score, change, trend)

        label = "🔥 AL" if score >= 8 else "👀 İZLE" if score >= 6 else "⚠️ ZAYIF"

        msg += f"{s}\n"
        msg += f"1D: {change:.2f}% | 7D Trend: {trend:.2f}%\n"
        msg += f"Score: {score} → {label}\n"
        msg += f"🧠 AI: {comment}\n\n"

    msg += "⚠️ Yatırım tavsiyesi değildir"

    send(msg)
else:
    send("Bugün güçlü AI sinyali yok.")
