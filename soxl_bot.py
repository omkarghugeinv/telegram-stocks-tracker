import asyncio
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
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
ALERT_COOLDOWN_MINUTES = float(os.environ.get("ALERT_COOLDOWN_MINUTES", "0"))
STALE_QUOTE_MINUTES = int(os.environ.get("STALE_QUOTE_MINUTES", "20"))

# Default charge settings (also adjustable anytime via /fees in Telegram)
DEFAULT_BROKERAGE_PCT = float(os.environ.get("BROKERAGE_PCT", "0.25"))  # per side (buy & sell)
DEFAULT_GST_PCT = float(os.environ.get("GST_PCT", "18.0"))              # GST on brokerage
DEFAULT_EXTRA_FEE_PCT = float(os.environ.get("FEE_PCT", "0.70"))        # extra/FX fees %
BROKERAGE_CAP_USD = float(os.environ.get("BROKERAGE_CAP_USD", "25.0"))  # max brokerage per order
YF_COOLDOWN_MINUTES = 15
MARKET_STATUS_CACHE_SECONDS = 300

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
    "last_alert_sent_at": 0.0,
    "market_status_checked_at": 0.0,
    "cached_session": "closed",
}


def fallback_market_session() -> str:
    now_ny = datetime.now(NY_TZ)
    if now_ny.weekday() >= 5:
        return "closed"
    mins = now_ny.hour * 60 + now_ny.minute
    if 9 * 60 + 30 <= mins < 16 * 60:
        return "regular"
    if (4 * 60 <= mins < 9 * 60 + 30) or (16 * 60 <= mins < 20 * 60):
        return "extended"
    return "closed"


def get_market_session() -> str:
    """Checks Finnhub market-status (cached 5m) to handle US holidays and half-days."""
    local_session = fallback_market_session()
    if local_session == "closed" or not FINNHUB_API_KEY:
        return local_session

    now = time.monotonic()
    if now - RUNTIME["market_status_checked_at"] < MARKET_STATUS_CACHE_SECONDS:
        return RUNTIME["cached_session"]

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/market-status",
            params={"exchange": "US"},
            headers={"X-Finnhub-Token": FINNHUB_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_session = (data.get("session") or "").lower()
        is_open = bool(data.get("isOpen"))

        if raw_session == "regular" or is_open:
            session = "regular"
        elif raw_session in ("pre-market", "post-market"):
            session = "extended"
        else:
            session = "closed"

        RUNTIME["cached_session"] = session
        RUNTIME["market_status_checked_at"] = now
        return session
    except Exception as e:
        print(f"Finnhub market-status check failed ({e}); using local ET schedule.")
        return local_session


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            merged = DEFAULT_STATE.copy()
            merged.update(data)
            return merged
        except Exception as e:
            print(f"Failed to read {STATE_FILE} ({e}); falling back to defaults.")
    save_state(DEFAULT_STATE)
    return DEFAULT_STATE.copy()


def save_state(state: dict) -> None:
    """Atomically writes position.json so a mid-write crash cannot corrupt state."""
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix="position.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def parse_positive(raw: str, label: str) -> float:
    val = float(raw)
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"{label} must be greater than 0.")
    return val


def parse_pct(raw: str, label: str, max_val: float = 100.0, allow_max: bool = False) -> float:
    val = float(raw)
    too_high = (val > max_val) if allow_max else (val >= max_val)
    if not math.isfinite(val) or val < 0 or too_high:
        bound = f"0–{max_val:g}%" if allow_max else f"0 to <{max_val:g}%"
        raise ValueError(f"{label} must be within {bound}.")
    return val


def fetch_finnhub_quote(ticker: str) -> tuple[float, datetime]:
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
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Invalid Finnhub quote response: {data}")
    ts_raw = data.get("t")
    quote_dt = (
        datetime.fromtimestamp(int(ts_raw), tz=NY_TZ)
        if ts_raw and int(ts_raw) > 0
        else datetime.now(NY_TZ)
    )
    return price, quote_dt


def fetch_yahoo_quote() -> tuple[float, datetime]:
    for period in ("1d", "5d"):
        df = YF_TICKER.history(period=period, interval="1m", prepost=True)
        if not df.empty:
            closes = df["Close"].dropna()
            if not closes.empty:
                price = float(closes.iloc[-1])
                raw_ts = closes.index[-1].to_pydatetime()
                if raw_ts.tzinfo is None:
                    raw_ts = raw_ts.replace(tzinfo=timezone.utc)
                return price, raw_ts.astimezone(NY_TZ)
    raise ValueError("No Yahoo Finance price data returned.")


def fetch_quote_for_session(session: str) -> tuple[float, datetime, str]:
    if session == "regular":
        try:
            price, qdt = fetch_finnhub_quote(TICKER)
            return price, qdt, "Finnhub"
        except Exception as e:
            print(f"Finnhub failed ({e}); falling back to Yahoo Finance...")
            price, qdt = fetch_yahoo_quote()
            return price, qdt, "Yahoo (Fallback)"

    try:
        price, qdt = fetch_yahoo_quote()
        return price, qdt, "Yahoo Extended"
    except Exception as e:
        if FINNHUB_API_KEY:
            print(f"Yahoo failed ({e}); falling back to Finnhub...")
            price, qdt = fetch_finnhub_quote(TICKER)
            return price, qdt, "Finnhub (Fallback)"
        raise


def is_quote_stale(quote_dt: datetime) -> bool:
    age_seconds = (datetime.now(NY_TZ) - quote_dt).total_seconds()
    return age_seconds > STALE_QUOTE_MINUTES * 60


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
    return "0% BE" if step == 0 else f"{step * MOVE_PCT:+g}%"


def format_report(price: float, quote_dt: datetime, state: dict, source: str) -> str:
    m = calculate_metrics(price, state)
    avg = state["avg_price"]
    true_be = m["true_be"]
    icon = "🟢" if m["net_pnl"] >= 0 else "🔴"
    brok_total = m["buy_brok"] + m["sell_brok"]
    ts_str = quote_dt.strftime("%b %d %H:%M ET")

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
        f"• Price: `${price:.2f}` (Avg: `${avg:.2f}`) • `{ts_str}`\n"
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
    try:
        session = await asyncio.to_thread(get_market_session)
        price, quote_dt, source = await asyncio.to_thread(fetch_quote_for_session, session)
        await update.message.reply_text(
            format_report(price, quote_dt, load_state(), f"{source} | {session}"),
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error fetching price: {e}")


async def avg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /avg <avg_price>"""
    if not is_authorized(update):
        return
    try:
        if len(context.args) != 1:
            raise ValueError("Usage: `/avg <avg_price>`\nExample: `/avg 45.82`")
        avg_price = parse_positive(context.args[0], "Average price")
        state = load_state()
        state["avg_price"] = avg_price
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Average Price Updated*\n{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="Markdown")


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /set <shares> <avg_price>"""
    if not is_authorized(update):
        return
    try:
        if len(context.args) != 2:
            raise ValueError("Usage: `/set <shares> <avg_price>`\nExample: `/set 13.32899018 45.82`")
        shares = parse_positive(context.args[0], "Shares")
        avg_price = parse_positive(context.args[1], "Average price")
        state = load_state()
        state["shares"] = shares
        state["avg_price"] = avg_price
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Position Updated*\n{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="Markdown")


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /buy <usd_spent> <buy_price>"""
    if not is_authorized(update):
        return
    try:
        if len(context.args) != 2:
            raise ValueError("Usage: `/buy <usd_spent> <buy_price>`\nExample: `/buy 50 42.00`")
        usd_spent = parse_positive(context.args[0], "USD spent")
        buy_price = parse_positive(context.args[1], "Buy price")
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
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="Markdown")


async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /fees <brokerage_per_side_%> <extra_fee_%> [gst_%]"""
    if not is_authorized(update):
        return
    try:
        if len(context.args) not in (2, 3):
            raise ValueError(
                "Usage: `/fees <brokerage_%> <extra_fee_%> [gst_%]`\n"
                "• Standard (`0.25%`/side + `0.70%` fees + `18%` GST): `/fees 0.25 0.70 18`\n"
                "• All-in `0.70%` only: `/fees 0 0.70 0`"
            )
        state = load_state()
        brok_pct = parse_pct(context.args[0], "Brokerage %", max_val=50.0)
        extra_pct = parse_pct(context.args[1], "Extra fee %", max_val=100.0)
        gst_pct = (
            parse_pct(context.args[2], "GST %", max_val=100.0, allow_max=True)
            if len(context.args) == 3
            else state["gst_pct"]
        )
        if (brok_pct / 100.0) * (1.0 + gst_pct / 100.0) >= 1.0:
            raise ValueError("Combined brokerage + GST cannot be 100% or higher.")

        state["brokerage_pct"] = brok_pct
        state["extra_fee_pct"] = extra_pct
        state["gst_pct"] = gst_pct
        state["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ *Charge Rates Updated*\n{format_breakeven_summary(state)}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="Markdown")


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

    session = await asyncio.to_thread(get_market_session)
    if session == "closed":
        return

    now = time.monotonic()

    if session == "regular":
        if now - RUNTIME["last_regular_check"] < (REGULAR_MINUTES * 60 - 5):
            return
        RUNTIME["last_regular_check"] = now
        try:
            price, quote_dt, source = await asyncio.to_thread(fetch_quote_for_session, "regular")
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
            price, quote_dt = await asyncio.to_thread(fetch_yahoo_quote)
            source = "Yahoo Extended"
        except Exception as e:
            RUNTIME["yf_cooldown_until"] = now + (YF_COOLDOWN_MINUTES * 60)
            print(f"Yahoo extended check failed ({e}); cooling down {YF_COOLDOWN_MINUTES}m.")
            return

    if is_quote_stale(quote_dt):
        print(f"Skipping stale {source} quote from {quote_dt.isoformat()}.")
        return

    m = calculate_metrics(price, state)
    true_be = m["true_be"]
    pct_from_be = m["pct_from_be"]
    last_step = state.get("last_be_step")

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
        # Optional anti-whipsaw cooldown (never delays True Breakeven 0% crossings)
        if (
            ALERT_COOLDOWN_MINUTES > 0
            and new_step != 0
            and (now - RUNTIME["last_alert_sent_at"]) < ALERT_COOLDOWN_MINUTES * 60
        ):
            return

        direction = "up" if new_step > last_step else "down"
        state["last_be_step"] = new_step
        save_state(state)
        RUNTIME["last_alert_sent_at"] = now

        if new_step == 0:
            header = (
                f"🎯 *True Breakeven Reached* (`0%` @ `${true_be:.2f}`)"
                if direction == "up"
                else f"⚠️ *Back to True Breakeven* (`0%` @ `${true_be:.2f}`)"
            )
        elif direction == "up":
            header = f"📈 *Crossed `{step_label(new_step)}` from Breakeven* (now `{pct_from_be:+.2f}%`)"
        else:
            header = f"📉 *Dropped to `{step_label(new_step)}` from Breakeven* (now `{pct_from_be:+.2f}%`)"

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"{header}\n\n{format_report(price, quote_dt, state, source)}",
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
        f"grid ±{MOVE_PCT:g}% from True BE | cooldown {ALERT_COOLDOWN_MINUTES:g}m)..."
    )
    app.run_polling()


if __name__ == "__main__":
    main()
