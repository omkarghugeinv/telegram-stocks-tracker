# Telegram Stocks Tracker (`@assist_stocks_bot`)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![Telegram Bot](https://img.shields.io/badge/Telegram-%40assist__stocks__bot-26A5E4?logo=telegram)](https://t.me/assist_stocks_bot)

An open-source, self-hostable Python Telegram bot for real-time US equity and ETF price monitoring, automated position breakeven calculations, and instant threshold alerts—powered by the Finnhub API and Yahoo Finance (`yfinance`).

Built to give individual traders reliable, zero-subscription live market updates directly in Telegram without relying on paid financial terminals or manual chart watching.

---

## Live Instance

- **Telegram Bot:** [@assist_stocks_bot](https://t.me/assist_stocks_bot)
- **Repository:** [github.com/omkarghugeinv/telegram-stocks-tracker](https://github.com/omkarghugeinv/telegram-stocks-tracker)

---

## Features

- **Real-Time Price Feed:** Fetches live market quotes via the Finnhub API with Yahoo Finance (`yfinance`) as a secondary/fallback data source.
- **Breakeven & Position Tracking:** Automatically computes average entry cost, breakeven levels, and live unrealized P&L for active positions (such as leveraged ETFs like `SOXL` or any supported US ticker).
- **Custom Price Alerts:** Sends instant Telegram notifications when a tracked ticker crosses configured target prices or percentage thresholds.
- **Rate-Limit Resilient:** Engineered to stay within free-tier API quotas (60 calls/min on Finnhub) with clean retry and backoff handling.
- **Containerized 24/7 Deployment:** Docker-ready for continuous uptime on any Linux VPS or self-hosted PaaS (such as Coolify or Oracle Cloud).

---

## Tech Stack

- **Language:** Python 3.10+
- **Messaging Interface:** Telegram Bot API
- **Market Data:** Finnhub API & `yfinance`
- **Deployment:** Docker / Docker Compose

---

## Bot Commands

| Command | Description |
| :--- | :--- |
| `/start` | Initialize `@assist_stocks_bot` and verify live status |
| `/price <TICKER>` | Fetch the latest live quote, day high/low, and percentage change |
| `/breakeven` | View current position summary, average entry price, and breakeven level |
| `/alert <TICKER> <PRICE>` | Set a live price threshold notification |
| `/status` | Check active data feed health and uptime |

---

## Quick Start

### 1. Clone the Repository

```bash
git clone [https://github.com/omkarghugeinv/telegram-stocks-tracker.git](https://github.com/omkarghugeinv/telegram-stocks-tracker.git)
cd telegram-stocks-tracker
```

### 2. Configure Environment Variables

Create a `.env` file in the project root (never commit your real `.env` file to Git):

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_chat_or_channel_id
FINNHUB_API_KEY=your_finnhub_api_key_here
DEFAULT_TICKER=SOXL
POLL_INTERVAL_SECONDS=15
```

### 3. Run Locally with Python

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 4. Run with Docker (Recommended for 24/7 Hosting)

```bash
docker build -t telegram-stocks-tracker .
docker run -d \
  --name telegram-stocks-tracker \
  --env-file .env \
  --restart unless-stopped \
  telegram-stocks-tracker
```

---

## Architecture Overview

1. **Data Ingestion Layer:** Retrieves live market quotes through Finnhub endpoints and falls back to Yahoo Finance when extended ticker metadata is needed.
2. **Evaluation Engine:** Compares incoming price updates against stored position cost bases and active user alert thresholds.
3. **Notification Dispatcher:** Pushes formatted alerts directly to Telegram via `@assist_stocks_bot` with cooldown throttling to prevent duplicate spam.

---

## Roadmap

- [ ] Persistent Finnhub WebSocket streaming for sub-second tick updates with automatic reconnection
- [ ] Multi-ticker watchlists per Telegram chat
- [ ] Automated `pytest` unit and integration test suite with GitHub Actions CI
- [ ] One-click `docker-compose.yml` self-hosting template

---

## Contributing

Contributions, bug reports, and feature requests are welcome:

1. Fork [omkarghugeinv/telegram-stocks-tracker](https://github.com/omkarghugeinv/telegram-stocks-tracker).
2. Create a feature branch (`git checkout -b feature/your-feature`).
3. Commit your changes with clear, descriptive messages.
4. Open a Pull Request explaining the improvement.

---

## Disclaimer

This software is provided for informational and educational purposes only and does not constitute financial or investment advice. Market data accuracy and availability depend on third-party upstream providers.

---

## License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for full details.
