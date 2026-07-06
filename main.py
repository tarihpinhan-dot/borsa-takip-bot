import os
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

data = {
    "chat_id": CHAT_ID,
    "text": "✅ Tebrikler! Bot başarıyla çalışıyor.\n\nBu mesaj GitHub Actions tarafından gönderildi."
}

response = requests.post(url, data=data)

print(response.text)
