# Live Stock & Position Alert Telegram Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)

An open-source, self-hostable Python Telegram bot for real-time US equity and ETF price monitoring, automated breakeven calculation, and instant threshold alerts—powered by the Finnhub API and Yahoo Finance (`yfinance`).

Built to give individual traders reliable, zero-subscription live updates directly in Telegram without relying on paid financial terminals or manual chart watching.

---

## Features

- **Real-Time Price Feed:** Streams/polls live market quotes using the Finnhub API with Yahoo Finance (`yfinance`) as a fallback data source.
- **Breakeven & Position Tracking:** Automatically calculates average entry price, breakeven levels, and live unrealized P&L for active positions (e.g., leveraged ETFs like `SOXL` or any supported ticker).
- **Custom Price Alerts:** Sends instant Telegram notifications when a ticker crosses configured target prices or percentage thresholds.
- **Rate-Limit Resilient:** Designed to operate comfortably within free-tier API quotas with clean retry and backoff handling.
- **Containerized 24/7 Deployment:** Ships ready for Docker and PaaS/VPS orchestrators (such as Coolify or Oracle Cloud VPS) for continuous uptime.

---

## Live Instance

- **Telegram Bot:** `https://t.me/YourBotUsername` *(replace with your live bot link or public channel)*

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
| `/start` | Initialize the bot and verify live connection |
| `/price <TICKER>` | Fetch the latest live quote, day high/low, and percentage change |
| `/breakeven` | View current position summary, average cost, and breakeven price |
| `/alert <TICKER> <PRICE>` | Set a live price threshold notification |
| `/status` | Check active data feed health and uptime |

*(Update this table to match the exact commands implemented in your script.)*

---

## Quick Start

### 1. Clone the Repository

```bash
git clone [https://github.com/omkarghugeinv/your-repo-name.git](https://github.com/omkarghugeinv/your-repo-name.git)
cd your-repo-name
```

### 2. Configure Environment Variables

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_chat_or_channel_id
FINNHUB_API_KEY=your_finnhub_api_key_here
DEFAULT_TICKER=SOXL
POLL_INTERVAL_SECONDS=15
```

### 3. Run with Python (Local)

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 4. Run with Docker (Recommended for 24/7 Hosting)

```bash
docker build -t stock-alert-bot .
docker run -d --name stock-alert-bot --env-file .env --restart unless-stopped stock-alert-bot
```

---

## Architecture Overview

1. **Data Ingestion Layer:** Fetches live market ticks via Finnhub REST/WebSocket endpoints and falls back to Yahoo Finance when extended metadata is needed.
2. **Evaluation Engine:** Compares incoming price updates against stored position cost bases and active user alert thresholds.
3. **Notification Dispatcher:** Pushes formatted updates to Telegram with cooldown throttling to prevent duplicate alert spam.

---

## Roadmap

- [ ] Persistent WebSocket streaming for sub-second tick updates
- [ ] Multi-ticker watchlist support per chat
- [ ] Automated unit and integration test suite (`pytest`)
- [ ] One-click `docker-compose.yml` deployment template

---

## Contributing

Contributions, bug reports, and feature requests are welcome:

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/your-feature`).
3. Commit your changes with clear messages.
4. Open a Pull Request describing the improvement.

---

## Disclaimer

This software is provided for informational and educational purposes only and does not constitute financial or investment advice. Market data accuracy depends on third-party upstream providers.

---

## License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for full details.
