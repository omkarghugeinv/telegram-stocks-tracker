import asyncio
import json
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

TICKER = os.environ.get("TICKER", "SOXL")
REGULAR_MINUTES = int(os.environ.get("REGULAR_MINUTES", "1"))
EXTENDED_MINUTES = int(os.environ.get("EXTENDED_MINUTES", "3"))
MOVE_PCT = float(os.environ.get("MOVE_PCT", "1.0"))

# Default charge settings (also adjustable anytime via /fees in Telegram)
DEFAULT_BROKERAGE_PCT = float(os.environ.get("BROKERAGE_PCT", "0.25"))  # per side (buy & sell)
DEFAULT_GST_PCT = float(os.environ.get("GST_PCT", "18.0"))              # GST on brokerage
DEFAULT_EXTRA_FEE_PCT = float(os.environ.get("FEE_PCT", "0.70"))        # extra/FX fees %
BROKERAGE_CAP_USD = float(os.environ.get("BROKERAGE_CAP_USD", "25.0"))  # max brokerage per order
YF_COOLDOWN_MINUTES = 15

NY_TZ = ZoneInfo("America/New_York")
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "position.json")

DEFAULT_STATE = {
    "shares": 13.32899018,
    "avg_price": 45.82,
    "brokerage_pct": DEFAULT_BROKERAGE_PCT,
    "gst_pct": DEFAULT_GST_PCT,
    "extra_fee_pct": DEFAULT_EXTRA_FEE_PCT,
    "alerts_enabled": True,
    "last_be_step": None,
}

YF_TICKER = yf.Ticker(TICKER)
RUNTIME = {
    "last_regular_check": 0.0,
    "last_extended_check": 0.0,
    "yf_cooldown_until": 0.0,
}


def get_market_session() -> str:
    now_ny = datetime.now(NY_TZ)
    if now_ny.weekday() >= 5:
        return "closed"
    mins = now_ny.hour * 60 + now_ny.minute
    if 9 * 60 + 30 <= mins < 16 * 60:
        return "regular"
    if (4 * 60 <= mins < 9 * 60 + 30) or (16 * 60 <= mins < 20 * 60):
        return "extended"
    return "closed"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        merged = DEFAULT_STATE.copy()
        merged.update(data)
        return merged
    save_state(DEFAULT_STATE)
    return DEFAULT_STATE.copy()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_finnhub_price(ticker: str) -> float:
    if not FINNHUB_API_KEY:
        raise ValueError("FINNHUB_API_KEY not configured")
    resp = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": ticker},
        headers={"X-Finnhub-Token": FINNHUB_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    price = float(data.get("c", 0))
    if price <= 0:
        raise ValueError(f"Invalid Finnhub quote response: {data}")
    return price


def fetch_yahoo_price() -> float:
    for period in ("1d", "5d"):
        df = YF_TICKER.history(period=period, interval="1m", prepost=True)
        if not df.empty:
            closes = df["Close"].dropna()
            if not closes.empty:
                return float(closes.iloc[-1])
    raise ValueError("No Yahoo Finance price data returned.")


def fetch_price_for_session(session: str) -> tuple[float, str]:
    if session == "regular":
        try:
            return fetch_finnhub_price(TICKER), "Finnhub"
        except Exception as e:
            print(f"Finnhub failed ({e}); falling back to Yahoo Finance...")
            return fetch_yahoo_price(), "Yahoo (Fallback)"

    try:
        return fetch_yahoo_price(), "Yahoo Extended"
    except Exception as e:
        if FINNHUB_API_KEY:
            print(f"Yahoo failed ({e}); falling back to Finnhub...")
            return fetch_finnhub_price(TICKER), "Finnhub (Fallback)"
        raise


def calculate_metrics(price: float, state: dict) -> dict:
    shares = state["shares"]
    avg = state["avg_price"]
    brok_rate = state["brokerage_pct"] / 100.0
    gst_mult = 1.0 + (state["gst_pct"] / 100.0)
    extra_rate = state["extra_fee_pct"] / 100.0

    invested = shares * avg
    current_val = shares * price

    buy_brok = min(invested * brok_rate, BROKERAGE_CAP_USD) * gst_mult
    sell_brok = min(current_val * brok_rate, BROKERAGE_CAP_USD) * gst_mult
    extra_fee = invested * extra_rate
    total_charges = buy_brok + sell_brok + extra_fee

    gross_pnl = current_val - invested
    net_pnl = gross_pnl - total_charges
    net_pnl_pct = (net_pnl / invested) * 100.0 if invested > 0 else 0.0

    b_eff = brok_rate * gst_mult
    needed_net = invested + buy_brok + extra_fee
    if shares > 0:
        if b_eff < 1.0:
            sell_val_needed = needed_net / (1.0 - b_eff)
            if sell_val_needed * brok_rate > BROKERAGE_CAP_USD:
                sell_val_needed = needed_net + (BROKERAGE_CAP_USD * gst_mult)
        else:
            sell_val_needed = needed_net
        true_be = sell_val_needed / shares
    else:
        true_be = 0.0

    pct_from_be = ((price - true_be) / true_be) * 100.0 if true_be > 0 else 0.0

    return {
        "invested": invested,
        "current_val": current_val,
        "buy_brok": buy_brok,
        "sell_brok": sell_brok,
        "extra_fee": extra_fee,
        "total_charges": total_charges,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "net_pnl_pct": net_pnl_pct,
        "true_be": true_be,
        "pct_from_be": pct_from_be,
        "to_pure_profit": true_be - price,
    }


def step_price(true_be: float, step: int) -> float:
    return true_be * (1.0 + (step * MOVE_PCT) / 100.0)


def step_label(step: int) -> str:
    if step == 0:
        return "0% BE"
    return f"{step * MOVE_PCT:+g}%"


def format_report(price: float, state: dict, source: str) -> str:
    m = calculate_metrics(price, state)
    avg = state["avg_price"]
    true_be = m["true_be"]
    icon = "🟢" if m["net_pnl"] >= 0 else "🔴"
    brok_total = m["buy_brok"] + m["sell_brok"]

    last_step = state.get("last_be_step")
    if last_step is not None and true_be > 0:
        down_step = last_step - 1
        up_step = last_step + 1
        next_line = (
            f"\n• Next Triggers: "
            f"📉 `${step_price(true_be, down_step):.2f}` (`{step_label(down_step)}`) | "
            f"📈 `${step_price(true_be, up_step):.2f}` (`{step_label(up_step)}`)"
        )
    else:
        next_line = ""

    return (
        f"{icon} *{TICKER} Position* ({source})\n"
        f"• Price: `${price:.2f}` (Avg: `${avg:.2f}`)\n"
        f"• True Breakeven: `${true_be:.2f}` (`{m['pct_from_be']:+.2f}%` from BE)\n"
        f"• Value: `${m['current_val']:.2f}` / `${m['invested']:.2f}`\n"
        f"• Net P&L: `${m['net_pnl']:+.2f}` (`{m['net_pnl_pct']:+.2f}%`)\n"
        f"• Gross P&L: `${m['gross_pnl']:+.2f}`\n"
        f"• Charges: `${m['total_charges']:.2f}` "
        f"(Brok+GST `${brok_total:.2f}` + Fees `${m['extra_fee']:.2f}`)\n"
        f"• To Pure Profit: `${m['to_pure_profit']:+.2f}`/share"
        f"{next_line}"
    )


def format_breakeven_summary(state: dict) -> str:
    m = calculate_metrics(state["avg_price"], state)
    true_be = m["true_be"]
    return (
        f"• Shares: `{state['shares']:.8f}`\n"
        f"• Raw Avg: `${state['avg_price']:.2f}`\n"
        f"• Brokerage: `{state['brokerage_pct']:g}%`/side + `{state['gst_pct']:g}%` GST "
        f"(`${m['buy_brok'] + m['sell_brok']:.2f}`)\n"
        f"• Extra Fees: `{state['extra_fee_pct']:g}%` (`${m['extra_fee']:.2f}`)\n"
        f"• *True Breakeven (0%):* `${true_be:.2f}`\n"
        f"• Grid Example: `-1%` `${step_price(true_be, -1):.2f}` | `+1%` `${step_price(true_be, 1):.2f}`"
    )


def is_authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    session = get_market_session()
    try:
        price, source = await asyncio.to_thread(fetch_price_for_session, session)
        await update.message.reply_text(
            format_report(price, load_state(), f"{source} | {session}"),
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error fetching price: {e}")


async def avg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /avg <avg_price>"""
    if not is_authorized(update):
        return
    try:
        avg_price = float(context.args[0])
        state = load_state()
        state["avg_price"] = avg_price
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Average Price Updated*\n{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/avg <avg_price>`\nExample: `/avg 45.82`",
            parse_mode="Markdown",
        )


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /set <shares> <avg_price>"""
    if not is_authorized(update):
        return
    try:
        shares = float(context.args[0])
        avg_price = float(context.args[1])
        state = load_state()
        state["shares"] = shares
        state["avg_price"] = avg_price
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Position Updated*\n{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/set <shares> <avg_price>`\nExample: `/set 13.32899018 45.82`",
            parse_mode="Markdown",
        )


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /buy <usd_spent> <buy_price>"""
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
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Added `${usd_spent:.2f}` @ `${buy_price:.2f}`*\n"
            f"{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError, ZeroDivisionError):
        await update.message.reply_text(
            "Usage: `/buy <usd_spent> <buy_price>`\nExample: `/buy 50 42.00`",
            parse_mode="Markdown",
        )


async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /fees <brokerage_per_side_pct> <extra_fee_pct>"""
    if not is_authorized(update):
        return
    try:
        brok_pct = float(context.args[0])
        extra_pct = float(context.args[1])
        state = load_state()
        state["brokerage_pct"] = brok_pct
        state["extra_fee_pct"] = extra_pct
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Charge Rates Updated*\n{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/fees <brokerage_per_side_%> <extra_fee_%>`\n"
            "• Standard (`0.25%`/side + `0.70%` fees): `/fees 0.25 0.70`\n"
            "• All-in `0.70%` only: `/fees 0 0.70`",
            parse_mode="Markdown",
        )


async def toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    state = load_state()
    state["alerts_enabled"] = not state.get("alerts_enabled", True)
    save_state(state)
    label = "ON" if state["alerts_enabled"] else "PAUSED"
    await update.message.reply_text(f"Trigger alerts are now *{label}*.", parse_mode="Markdown")


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    if not state.get("alerts_enabled", True):
        return

    session = get_market_session()
    if session == "closed":
        return

    now = time.monotonic()

    if session == "regular":
        if now - RUNTIME["last_regular_check"] < (REGULAR_MINUTES * 60 - 5):
            return
        RUNTIME["last_regular_check"] = now
        try:
            price, source = await asyncio.to_thread(fetch_price_for_session, "regular")
        except Exception as e:
            print(f"Regular-hours price check failed: {e}")
            return
    else:
        if now < RUNTIME["yf_cooldown_until"]:
            return
        if now - RUNTIME["last_extended_check"] < (EXTENDED_MINUTES * 60 - 5):
            return
        RUNTIME["last_extended_check"] = now
        try:
            price = await asyncio.to_thread(fetch_yahoo_price)
            source = "Yahoo Extended"
        except Exception as e:
            RUNTIME["yf_cooldown_until"] = now + (YF_COOLDOWN_MINUTES * 60)
            print(f"Yahoo extended check failed ({e}); cooling down {YF_COOLDOWN_MINUTES}m.")
            return

    m = calculate_metrics(price, state)
    true_be = m["true_be"]
    pct_from_be = m["pct_from_be"]
    last_step = state.get("last_be_step")

    # First run (or right after /avg, /set, /buy, /fees): lock onto nearest breakeven grid step
    if last_step is None:
        current_step = int(round(pct_from_be / MOVE_PCT))
        state["last_be_step"] = current_step
        save_state(state)
        down_step = current_step - 1
        up_step = current_step + 1
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"📌 *{TICKER} Breakeven Grid Armed* ({source})\n"
                f"• Price: `${price:.2f}` (`{pct_from_be:+.2f}%` from `${true_be:.2f}` BE)\n"
                f"• Next Down Alert: `${step_price(true_be, down_step):.2f}` (`{step_label(down_step)}`)\n"
                f"• Next Up Alert: `${step_price(true_be, up_step):.2f}` (`{step_label(up_step)}`)"
            ),
            parse_mode="Markdown",
        )
        return

    eps = 1e-9
    new_step = None
    if pct_from_be >= (last_step + 1) * MOVE_PCT - eps:
        new_step = math.floor((pct_from_be + eps) / MOVE_PCT)
    elif pct_from_be <= (last_step - 1) * MOVE_PCT + eps:
        new_step = math.ceil((pct_from_be - eps) / MOVE_PCT)

    if new_step is not None and new_step != last_step:
        direction = "up" if new_step > last_step else "down"
        state["last_be_step"] = new_step
        save_state(state)

        if new_step == 0:
            if direction == "up":
                header = f"🎯 *True Breakeven Reached* (`0%` @ `${true_be:.2f}`)"
            else:
                header = f"⚠️ *Back to True Breakeven* (`0%` @ `${true_be:.2f}`)"
        elif direction == "up":
            header = f"📈 *Crossed `{step_label(new_step)}` from Breakeven* (now `{pct_from_be:+.2f}%`)"
        else:
            header = f"📉 *Dropped to `{step_label(new_step)}` from Breakeven* (now `{pct_from_be:+.2f}%`)"

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"{header}\n\n{format_report(price, state, source)}",
            parse_mode="Markdown",
        )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("avg", avg_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("toggle", toggle_cmd))

    app.job_queue.run_repeating(scheduled_check, interval=60, first=10)
    print(
        f"{TICKER} hybrid bot started "
        f"(Finnhub every {REGULAR_MINUTES}m regular | Yahoo every {EXTENDED_MINUTES}m extended | "
        f"grid ±{MOVE_PCT:g}% from True BE)..."
    )
    app.run_polling()


if __name__ == "__main__":
    main()
