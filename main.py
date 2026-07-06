from scanner import scan_market
from notifier import send_telegram_message


def format_report(results):
    if not results:
        return "Bugün day trading için güçlü aday bulunamadı."

    message = "🚀 AI DAY TRADING RADAR\n\n"

    for i, item in enumerate(results, start=1):
        message += f"{i}) {item['symbol']}\n"
        message += f"Skor: {item['score']}/100\n"
        message += f"Fiyat: ${item['price']:.2f}\n"
        message += f"1 Gün: {item['daily_change']:.2f}%\n"
        message += f"5 Gün Trend: {item['trend_5d']:.2f}%\n"
        message += f"Hacim Oranı: {item['volume_ratio']:.2f}x\n"
        message += f"Volatilite: {item['volatility']:.2f}%\n\n"

    message += "⚠️ Bu bir yatırım tavsiyesi değildir."
    return message


def main():
    results = scan_market()
    report = format_report(results)
    send_telegram_message(report)


if __name__ == "__main__":
    main()
