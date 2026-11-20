import asyncio
import json
import os
from datetime import datetime, timezone
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
TICKER = os.environ.get("TICKER", "SOXL")
UPDATE_MINUTES = int(os.environ.get("UPDATE_MINUTES", "30"))

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "position.json")

DEFAULT_STATE = {
    "shares": 15.73700413,
    "avg_price": 45.12,
    "alerts_enabled": True,
}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    save_state(DEFAULT_STATE)
    return DEFAULT_STATE.copy()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_latest_price(ticker: str) -> float:
    df = yf.Ticker(ticker).history(period="5d", interval="1m", prepost=True)
    closes = df["Close"].dropna()
    if closes.empty:
        raise ValueError("No price data returned.")
    return float(closes.iloc[-1])


def format_report(price: float, state: dict) -> str:
    shares = state["shares"]
    avg = state["avg_price"]
    invested = shares * avg
    current_val = shares * price
    pnl_usd = current_val - invested
    pnl_pct = ((price - avg) / avg) * 100 if avg > 0 else 0.0
    to_breakeven = avg - price
    icon = "🟢" if pnl_usd >= 0 else "🔴"

    return (
        f"{icon} *{TICKER} Update*\n"
        f"• Price: `${price:.2f}` (Avg: `${avg:.2f}`)\n"
        f"• Shares: `{shares:.8f}`\n"
        f"• Value: `${current_val:.2f}` / `${invested:.2f}`\n"
        f"• P&L: `${pnl_usd:+.2f}` (`{pnl_pct:+.2f}%`)\n"
        f"• To Breakeven: `${to_breakeven:+.2f}`/share"
    )


def is_authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    try:
        price = await asyncio.to_thread(fetch_latest_price, TICKER)
        await update.message.reply_text(format_report(price, load_state()), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error fetching price: {e}")


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    try:
        shares = float(context.args[0])
        avg_price = float(context.args[1])
        state = load_state()
        state["shares"] = shares
        state["avg_price"] = avg_price
        save_state(state)
        await update.message.reply_text(
            f"✅ Position updated: `{shares}` shares @ `${avg_price:.2f}`",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/set <shares> <avg_price>`\nExample: `/set 15.58 45.60`",
            parse_mode="Markdown",
        )


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    try:
        usd_spent = float(context.args[0])
        buy_price = float(context.args[1])
        state = load_state()
        old_cost = state["shares"] * state["avg_price"]
        new_shares = state["shares"] + (usd_spent / buy_price)
        new_avg = (old_cost + usd_spent) / new_shares
        state["shares"] = new_shares
        state["avg_price"] = new_avg
        save_state(state)
        await update.message.reply_text(
            f"✅ Added `${usd_spent:.2f}` @ `${buy_price:.2f}`\n"
            f"• New Shares: `{new_shares:.8f}`\n"
            f"• New Avg: `${new_avg:.2f}`",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError, ZeroDivisionError):
        await update.message.reply_text(
            "Usage: `/buy <usd_spent> <buy_price>`\nExample: `/buy 50 42.00`",
            parse_mode="Markdown",
        )


async def toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    state = load_state()
    state["alerts_enabled"] = not state.get("alerts_enabled", True)
    save_state(state)
    label = "ON" if state["alerts_enabled"] else "PAUSED"
    await update.message.reply_text(f"Scheduled alerts are now *{label}*.", parse_mode="Markdown")


async def scheduled_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    if not state.get("alerts_enabled", True):
        return
    if datetime.now(timezone.utc).weekday() >= 5:
        return
    try:
        price = await asyncio.to_thread(fetch_latest_price, TICKER)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=format_report(price, state),
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"Scheduled update failed: {e}")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("toggle", toggle_cmd))

    app.job_queue.run_repeating(scheduled_update, interval=UPDATE_MINUTES * 60, first=10)
    print(f"{TICKER} Telegram bot started (interval: {UPDATE_MINUTES}m)...")
    app.run_polling()


if __name__ == "__main__":
    main()
