# AstraTrade AI Windows EXE
Build is performed by GitHub Actions on a Windows runner.
Download the AstraTradeAI-Windows artifact from Actions after a successful build.
Run the EXE, enter Alpaca PAPER API credentials, test connection, then start the bot.
Only the paper API endpoint is used for order submissions; no live-trading mode exists.
The app scans active tradable US equities in rotating batches of 30 using IEX daily bars.
The hybrid score combines technical indicators, an experimental in-process logistic baseline, and keyword news sentiment.
This model is not validated for profitability. Risk controls limit positions and order sizing but cannot prevent losses.
The PC and app must stay on. This is a desktop app, not a cloud 24/7 service. API keys are held in memory only.
