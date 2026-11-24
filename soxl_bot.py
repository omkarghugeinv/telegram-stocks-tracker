import asyncio
import html
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
import yfinance as yf
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

DEFAULT_TICKER = os.environ.get("TICKER", "SOXL").strip().upper()
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
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "position.json")

TICKER_RE = re.compile(r"^[A-Z0-9.\-^]{1,12}$")

DEFAULT_STATE = {
    "brokerage_pct": DEFAULT_BROKERAGE_PCT,
    "gst_pct": DEFAULT_GST_PCT,
    "extra_fee_pct": DEFAULT_EXTRA_FEE_PCT,
    "alerts_enabled": True,
    "positions": {
        DEFAULT_TICKER: {
            "shares": 13.32899018,
            "avg_price": 45.82,
            "last_be_step": None,
        }
    },
}

YF_TICKERS: dict[str, yf.Ticker] = {}
RUNTIME = {
    "last_regular_check": 0.0,
    "last_extended_check": 0.0,
    "yf_cooldown_until": 0.0,
    "last_alert_sent_at": {},
    "market_status_checked_at": 0.0,
    "cached_session": "closed",
}


def get_yf_ticker(symbol: str) -> yf.Ticker:
    if symbol not in YF_TICKERS:
        YF_TICKERS[symbol] = yf.Ticker(symbol)
    return YF_TICKERS[symbol]


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
    """Loads position.json and automatically migrates legacy single-ticker state."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)

            # Migrate old flat single-ticker format -> multi-ticker format
            if "positions" not in data:
                migrated = {
                    "brokerage_pct": float(data.get("brokerage_pct", DEFAULT_BROKERAGE_PCT)),
                    "gst_pct": float(data.get("gst_pct", DEFAULT_GST_PCT)),
                    "extra_fee_pct": float(data.get("extra_fee_pct", DEFAULT_EXTRA_FEE_PCT)),
                    "alerts_enabled": bool(data.get("alerts_enabled", True)),
                    "positions": {},
                }
                shares = float(data.get("shares", 0))
                avg_price = float(data.get("avg_price", 0))
                if shares > 0 and avg_price > 0:
                    migrated["positions"][DEFAULT_TICKER] = {
                        "shares": shares,
                        "avg_price": avg_price,
                        "last_be_step": data.get("last_be_step"),
                    }
                save_state(migrated)
                print(f"Migrated legacy position.json to multi-ticker format ({list(migrated['positions'].keys())}).")
                return migrated

            state = {
                "brokerage_pct": float(data.get("brokerage_pct", DEFAULT_BROKERAGE_PCT)),
                "gst_pct": float(data.get("gst_pct", DEFAULT_GST_PCT)),
                "extra_fee_pct": float(data.get("extra_fee_pct", DEFAULT_EXTRA_FEE_PCT)),
                "alerts_enabled": bool(data.get("alerts_enabled", True)),
                "positions": {},
            }
            for sym, pos in (data.get("positions") or {}).items():
                if not isinstance(pos, dict):
                    continue
                s = float(pos.get("shares", 0))
                a = float(pos.get("avg_price", 0))
                if s > 0 and a > 0:
                    state["positions"][sym.upper()] = {
                        "shares": s,
                        "avg_price": a,
                        "last_be_step": pos.get("last_be_step"),
                    }
            return state
        except Exception as e:
            print(f"Failed to read {STATE_FILE} ({e}); falling back to defaults.")

    fresh = json.loads(json.dumps(DEFAULT_STATE))
    save_state(fresh)
    return fresh


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


def parse_ticker(raw: str) -> str:
    sym = raw.strip().lstrip("$").upper()
    if not TICKER_RE.match(sym):
        raise ValueError(f"Invalid ticker symbol: <code>{html.escape(raw)}</code>")
    return sym


def parse_positive(raw: str, label: str) -> float:
    val = float(raw)
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"{label} must be greater than 0.")
    return val


def parse_pct(raw: str, label: str, max_val: float = 100.0, allow_max: bool = False) -> float:
    val = float(raw)
    too_high = (val > max_val) if allow_max else (val >= max_val)
    if not math.isfinite(val) or val < 0 or too_high:
        bound = f"0–{max_val:g}%" if allow_max else f"0 to &lt;{max_val:g}%"
        raise ValueError(f"{label} must be within {bound}.")
    return val


def fmt_signed_usd(val: float) -> str:
    if abs(val) < 0.005:
        val = 0.0
    return f"{'+' if val >= 0 else '-'}${abs(val):.2f}"


def is_rate_limit_error(err: Exception) -> bool:
    msg = str(err).lower()
    return "429" in msg or "rate" in msg or "too many requests" in msg


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
        raise ValueError(f"No valid Finnhub quote for {ticker}")
    ts_raw = data.get("t")
    quote_dt = (
        datetime.fromtimestamp(int(ts_raw), tz=NY_TZ)
        if ts_raw and int(ts_raw) > 0
        else datetime.now(NY_TZ)
    )
    return price, quote_dt


def fetch_yahoo_quote(ticker: str) -> tuple[float, datetime]:
    t = get_yf_ticker(ticker)
    for period in ("1d", "5d"):
        df = t.history(period=period, interval="1m", prepost=True)
        if not df.empty:
            closes = df["Close"].dropna()
            if not closes.empty:
                price = float(closes.iloc[-1])
                raw_ts = closes.index[-1].to_pydatetime()
                if raw_ts.tzinfo is None:
                    raw_ts = raw_ts.replace(tzinfo=timezone.utc)
                return price, raw_ts.astimezone(NY_TZ)
    raise ValueError(f"No Yahoo Finance data for {ticker}")


def fetch_quote_for_session(ticker: str, session: str) -> tuple[float, datetime, str]:
    if session == "regular":
        try:
            price, qdt = fetch_finnhub_quote(ticker)
            return price, qdt, "Finnhub"
        except Exception as e:
            print(f"[{ticker}] Finnhub failed ({e}); falling back to Yahoo Finance...")
            price, qdt = fetch_yahoo_quote(ticker)
            return price, qdt, "Yahoo (Fallback)"

    yahoo_label = "Yahoo Extended" if session == "extended" else "Yahoo (Closed)"
    try:
        price, qdt = fetch_yahoo_quote(ticker)
        return price, qdt, yahoo_label
    except Exception as e:
        if FINNHUB_API_KEY:
            print(f"[{ticker}] Yahoo failed ({e}); falling back to Finnhub...")
            price, qdt = fetch_finnhub_quote(ticker)
            fb_label = "Finnhub (Fallback)" if session == "extended" else "Finnhub (Closed)"
            return price, qdt, fb_label
        raise


def is_quote_stale(quote_dt: datetime) -> bool:
    age_seconds = (datetime.now(NY_TZ) - quote_dt).total_seconds()
    return age_seconds > STALE_QUOTE_MINUTES * 60


def calculate_metrics(price: float, pos: dict, state: dict) -> dict:
    shares = pos["shares"]
    avg = pos["avg_price"]
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
        "total_charges": total_charges,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "net_pnl_pct": net_pnl_pct,
        "true_be": true_be,
        "pct_from_be": pct_from_be,
    }


def step_price(true_be: float, step: int) -> float:
    return true_be * (1.0 + (step * MOVE_PCT) / 100.0)


def step_label(step: int) -> str:
    return "0% BE" if step == 0 else f"{step * MOVE_PCT:+g}%"


def build_status_keyboard(state: dict) -> InlineKeyboardMarkup:
    alert_btn = "🔔 Alerts: ON" if state.get("alerts_enabled", True) else "🔕 Paused"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
            InlineKeyboardButton(alert_btn, callback_data="toggle_status"),
            InlineKeyboardButton("⚙️ Commands", callback_data="help"),
        ]
    ])


def build_help_keyboard(state: dict) -> InlineKeyboardMarkup:
    alert_btn = "🔔 Alerts: ON" if state.get("alerts_enabled", True) else "🔕 Paused"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Live Status", callback_data="refresh"),
            InlineKeyboardButton(alert_btn, callback_data="toggle_help"),
        ]
    ])


def format_single_card(
    ticker: str,
    price: float,
    quote_dt: datetime,
    pos: dict,
    state: dict,
    source: str,
    compact: bool = False,
    header: str | None = None,
) -> str:
    m = calculate_metrics(price, pos, state)
    true_be = m["true_be"]
    icon = "🟢" if m["net_pnl"] >= 0 else "🔴"
    net_str = fmt_signed_usd(m["net_pnl"])
    safe_sym = html.escape(ticker)
    safe_source = html.escape(source)

    last_step = pos.get("last_be_step")
    if last_step is not None and true_be > 0:
        down_p = step_price(true_be, last_step - 1)
        up_p = step_price(true_be, last_step + 1)
        next_short = f" • Next: <code>${down_p:.2f}</code> / <code>${up_p:.2f}</code>"
        next_line = (
            f"\n📉 <code>${down_p:.2f}</code> (<code>{step_label(last_step - 1)}</code>)  |  "
            f"📈 <code>${up_p:.2f}</code> (<code>{step_label(last_step + 1)}</code>)"
        )
    else:
        next_short = ""
        next_line = ""

    if compact:
        title = f"{header}\n" if header else ""
        return (
            f"{title}"
            f"<blockquote>{icon} <b>{safe_sym}</b>  <code>${price:.2f}</code>  "
            f"(<code>{m['pct_from_be']:+.2f}%</code> vs BE)\n"
            f"<b>Net P&amp;L:</b> <code>{net_str}</code> (<code>{m['net_pnl_pct']:+.2f}%</code>)\n"
            f"<b>BE:</b> <code>${true_be:.2f}</code>{next_short}</blockquote>"
        )

    ts_str = quote_dt.strftime("%b %d, %H:%M ET")
    return (
        f"{icon} <b>{safe_sym}</b>  <code>${price:.2f}</code>\n"
        f"<i>{ts_str} • {safe_source}</i>\n"
        f"<blockquote><b>Net P&amp;L:</b> <code>{net_str}</code> (<code>{m['net_pnl_pct']:+.2f}%</code>)\n"
        f"<b>Value:</b> <code>${m['current_val']:.2f}</code> / <code>${m['invested']:.2f}</code>\n"
        f"<b>True BE:</b> <code>${true_be:.2f}</code> (<code>{m['pct_from_be']:+.2f}%</code> • Avg <code>${pos['avg_price']:.2f}</code>)\n"
        f"<b>Fees:</b> <code>${m['total_charges']:.2f}</code></blockquote>"
        f"{next_line}"
    )


def format_portfolio_status(state: dict, quotes: dict[str, tuple[float, datetime, str]], errors: dict[str, str]) -> str:
    positions = state["positions"]
    if not positions:
        return (
            "📭 <b>No Active Positions</b>\n"
            "<blockquote>Add a stock or ETF anytime:\n"
            "• <code>/add TQQQ 10 75.50</code>\n"
            "• <code>/buy SOXL 100 41.00</code></blockquote>"
        )

    # Single holding: show the classic clean single-ticker card
    if len(positions) == 1:
        sym, pos = next(iter(positions.items()))
        if sym in quotes:
            price, qdt, src = quotes[sym]
            return format_single_card(sym, price, qdt, pos, state, src, compact=False)
        return f"⚠️ <b>{html.escape(sym)}</b> fetch failed: {html.escape(errors.get(sym, 'Unknown error'))}"

    # Multiple holdings: portfolio total header + one compact card per ticker
    total_val = 0.0
    total_inv = 0.0
    total_net = 0.0
    latest_dt = None
    last_src = ""
    blocks = []

    for sym, pos in positions.items():
        safe_sym = html.escape(sym)
        if sym not in quotes:
            err = html.escape(errors.get(sym, "Quote unavailable"))
            blocks.append(f"<blockquote>⚠️ <b>{safe_sym}</b> — {err}</blockquote>")
            continue

        price, qdt, src = quotes[sym]
        if latest_dt is None or qdt > latest_dt:
            latest_dt = qdt
            last_src = src

        m = calculate_metrics(price, pos, state)
        total_val += m["current_val"]
        total_inv += m["invested"]
        total_net += m["net_pnl"]

        icon = "🟢" if m["net_pnl"] >= 0 else "🔴"
        net_str = fmt_signed_usd(m["net_pnl"])
        true_be = m["true_be"]

        last_step = pos.get("last_be_step")
        if last_step is not None and true_be > 0:
            down_p = step_price(true_be, last_step - 1)
            up_p = step_price(true_be, last_step + 1)
            next_line = (
                f"\n📉 <code>${down_p:.2f}</code> (<code>{step_label(last_step - 1)}</code>) | "
                f"📈 <code>${up_p:.2f}</code> (<code>{step_label(last_step + 1)}</code>)"
            )
        else:
            next_line = ""

        blocks.append(
            f"<blockquote>{icon} <b>{safe_sym}</b>  <code>${price:.2f}</code>  "
            f"(<code>{m['pct_from_be']:+.2f}%</code> vs BE)\n"
            f"<b>Net:</b> <code>{net_str}</code> (<code>{m['net_pnl_pct']:+.2f}%</code>) • "
            f"<b>BE:</b> <code>${true_be:.2f}</code> (Avg <code>${pos['avg_price']:.2f}</code>)"
            f"{next_line}</blockquote>"
        )

    tot_pct = (total_net / total_inv) * 100.0 if total_inv > 0 else 0.0
    tot_icon = "🟢" if total_net >= 0 else "🔴"
    meta = f"{latest_dt.strftime('%b %d, %H:%M ET')} • {html.escape(last_src)}" if latest_dt else "Unavailable"

    header = (
        f"{tot_icon} <b>Portfolio ({len(positions)})</b>\n"
        f"<i>{meta}</i>\n"
        f"<b>Total Net:</b> <code>{fmt_signed_usd(total_net)}</code> (<code>{tot_pct:+.2f}%</code>) • "
        f"<b>Val:</b> <code>${total_val:.2f}</code> / <code>${total_inv:.2f}</code>\n"
    )
    return header + "\n".join(blocks)


def format_breakeven_summary(ticker: str, state: dict) -> str:
    pos = state["positions"][ticker]
    m = calculate_metrics(pos["avg_price"], pos, state)
    true_be = m["true_be"]
    return (
        f"<blockquote><b>Ticker:</b> <code>{html.escape(ticker)}</code>\n"
        f"<b>Shares:</b> <code>{pos['shares']:.8f}</code>  |  <b>Avg:</b> <code>${pos['avg_price']:.2f}</code>\n"
        f"<b>Fees:</b> <code>${m['total_charges']:.2f}</code> "
        f"(<code>{state['brokerage_pct']:g}%</code> + <code>{state['gst_pct']:g}%</code> GST, "
        f"<code>{state['extra_fee_pct']:g}%</code> extra)\n"
        f"<b>True BE:</b> <code>${true_be:.2f}</code> "
        f"(<code>-1%</code> <code>${step_price(true_be, -1):.2f}</code> | "
        f"<code>+1%</code> <code>${step_price(true_be, 1):.2f}</code>)</blockquote>"
    )


def format_help(state: dict) -> str:
    positions = state["positions"]
    if positions:
        holding_lines = []
        for sym, pos in positions.items():
            be = calculate_metrics(pos["avg_price"], pos, state)["true_be"]
            holding_lines.append(
                f"• <b>{html.escape(sym)}</b>: <code>{pos['shares']:.4f}</code> sh @ "
                f"<code>${pos['avg_price']:.2f}</code> (BE <code>${be:.2f}</code>)"
            )
        holdings_block = "\n".join(holding_lines)
        sample_sym = next(iter(positions))
    else:
        holdings_block = "• <i>None (alerts idle)</i>"
        sample_sym = "SOXL"

    return (
        f"⚙️ <b>Portfolio Tracker Settings &amp; Commands</b>\n"
        f"<blockquote><b>Holdings:</b>\n{holdings_block}\n"
        f"<b>Grid:</b> <code>±{MOVE_PCT:g}%</code> from BE  |  "
        f"<b>Charges:</b> <code>{state['brokerage_pct']:g}%</code> + <code>{state['gst_pct']:g}%</code> GST + <code>{state['extra_fee_pct']:g}%</code> extra</blockquote>\n"
        f"<b>Tap any example to copy:</b>\n"
        f"• <code>/status</code> — Live portfolio P&amp;L &amp; triggers\n"
        f"• <code>/add TQQQ 10 75.50</code> — Add a new ticker (or overwrite)\n"
        f"• <code>/buy {sample_sym} 100 41.00</code> — Log a buy (USD &amp; price)\n"
        f"• <code>/sell {sample_sym}</code> — Remove sold ticker (no record)\n"
        f"• <code>/sell {sample_sym} 5</code> — Trim 5 shares only\n"
        f"• <code>/avg {sample_sym} 45.12</code> — Update avg price\n"
        f"• <code>/fees {state['brokerage_pct']:g} {state['extra_fee_pct']:g} {state['gst_pct']:g}</code> — Update brok / extra / GST %\n"
        f"• <code>/toggle</code> — Pause or resume alerts"
    )


def is_authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


def fetch_quotes_batch(tickers: list[str], session: str) -> tuple[dict, dict]:
    quotes = {}
    errors = {}
    for idx, sym in enumerate(tickers):
        if idx > 0:
            time.sleep(0.25)
        try:
            quotes[sym] = fetch_quote_for_session(sym, session)
        except Exception as e:
            errors[sym] = str(e)
    return quotes, errors


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    state = load_state()
    positions = state["positions"]

    if not positions:
        await update.message.reply_text(
            format_portfolio_status(state, {}, {}),
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
        return

    try:
        session = await asyncio.to_thread(get_market_session)
        if context.args:
            sym = parse_ticker(context.args[0])
            if sym not in positions:
                await update.message.reply_text(
                    f"<code>{html.escape(sym)}</code> is not in your portfolio. Use <code>/add {html.escape(sym)} &lt;shares&gt; &lt;avg&gt;</code>.",
                    parse_mode="HTML",
                )
                return
            price, qdt, src = await asyncio.to_thread(fetch_quote_for_session, sym, session)
            await update.message.reply_text(
                format_single_card(sym, price, qdt, positions[sym], state, src, compact=False),
                parse_mode="HTML",
                reply_markup=build_status_keyboard(state),
            )
            return

        quotes, errors = await asyncio.to_thread(fetch_quotes_batch, list(positions.keys()), session)
        await update.message.reply_text(
            format_portfolio_status(state, quotes, errors),
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
    except Exception as e:
        await update.message.reply_text(f"Error fetching status: {html.escape(str(e))}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    state = load_state()
    await update.message.reply_text(
        format_help(state),
        parse_mode="HTML",
        reply_markup=build_help_keyboard(state),
    )


async def add_or_set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /add <TICKER> <shares> <avg_price> (or /set <shares> <avg> if only 1 holding)."""
    if not is_authorized(update):
        return
    try:
        state = load_state()
        args = context.args
        if len(args) == 3:
            sym = parse_ticker(args[0])
            shares = parse_positive(args[1], "Shares")
            avg_price = parse_positive(args[2], "Average price")
        elif len(args) == 2 and len(state["positions"]) == 1:
            sym = next(iter(state["positions"]))
            shares = parse_positive(args[0], "Shares")
            avg_price = parse_positive(args[1], "Average price")
        else:
            raise ValueError(
                "Usage: <code>/add &lt;TICKER&gt; &lt;shares&gt; &lt;avg_price&gt;</code>\n"
                "Example: <code>/add TQQQ 10 75.50</code>"
            )

        is_new = sym not in state["positions"]
        state["positions"][sym] = {
            "shares": shares,
            "avg_price": avg_price,
            "last_be_step": None,
        }
        save_state(state)
        action = "Added" if is_new else "Updated"
        await update.message.reply_text(
            f"✅ <b>{action} {html.escape(sym)}</b>\n{format_breakeven_summary(sym, state)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /buy <TICKER> <usd_spent> <buy_price> (creates ticker automatically if new)."""
    if not is_authorized(update):
        return
    try:
        state = load_state()
        args = context.args
        if len(args) == 3:
            sym = parse_ticker(args[0])
            usd_spent = parse_positive(args[1], "USD spent")
            buy_price = parse_positive(args[2], "Buy price")
        elif len(args) == 2 and len(state["positions"]) == 1:
            sym = next(iter(state["positions"]))
            usd_spent = parse_positive(args[0], "USD spent")
            buy_price = parse_positive(args[1], "Buy price")
        else:
            raise ValueError(
                "Usage: <code>/buy &lt;TICKER&gt; &lt;usd_spent&gt; &lt;buy_price&gt;</code>\n"
                "Example: <code>/buy SOXL 100 41.00</code>"
            )

        added_shares = usd_spent / buy_price
        if sym in state["positions"]:
            pos = state["positions"][sym]
            old_cost = pos["shares"] * pos["avg_price"]
            new_shares = pos["shares"] + added_shares
            new_avg = (old_cost + usd_spent) / new_shares
        else:
            new_shares = added_shares
            new_avg = buy_price

        state["positions"][sym] = {
            "shares": new_shares,
            "avg_price": new_avg,
            "last_be_step": None,
        }
        save_state(state)
        await update.message.reply_text(
            f"✅ <b>Bought <code>${usd_spent:.2f}</code> of {html.escape(sym)} @ <code>${buy_price:.2f}</code></b>\n"
            f"{format_breakeven_summary(sym, state)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /sell <TICKER> (removes completely, no record) or /sell <TICKER> <shares> (partial)."""
    if not is_authorized(update):
        return
    try:
        if len(context.args) not in (1, 2):
            raise ValueError(
                "Usage:\n"
                "• Sold all (remove completely): <code>/sell SOXL</code>\n"
                "• Sold partial shares: <code>/sell SOXL 5</code>"
            )
        state = load_state()
        sym = parse_ticker(context.args[0])
        if sym not in state["positions"]:
            raise ValueError(f"<code>{html.escape(sym)}</code> is not in your portfolio.")

        if len(context.args) == 1:
            del state["positions"][sym]
            save_state(state)
            RUNTIME["last_alert_sent_at"].pop(sym, None)
            YF_TICKERS.pop(sym, None)
            remaining = len(state["positions"])
            note = "All positions cleared — polling paused until you add a ticker." if remaining == 0 else f"Tracking <b>{remaining}</b> remaining holding(s)."
            await update.message.reply_text(
                f"🗑️ <b>Removed {html.escape(sym)}</b> (no record kept).\n{note}",
                parse_mode="HTML",
                reply_markup=build_status_keyboard(state),
            )
            return

        sold_shares = parse_positive(context.args[1], "Shares sold")
        pos = state["positions"][sym]
        new_shares = pos["shares"] - sold_shares
        if new_shares <= 1e-8:
            del state["positions"][sym]
            save_state(state)
            RUNTIME["last_alert_sent_at"].pop(sym, None)
            YF_TICKERS.pop(sym, None)
            await update.message.reply_text(
                f"🗑️ <b>Removed {html.escape(sym)}</b> (position fully closed, no record kept).",
                parse_mode="HTML",
                reply_markup=build_status_keyboard(state),
            )
            return

        pos["shares"] = new_shares
        pos["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ <b>Trimmed <code>{sold_shares:g}</code> shares of {html.escape(sym)}</b>\n"
            f"{format_breakeven_summary(sym, state)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def avg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /avg <TICKER> <avg_price> (or /avg <price> if only 1 holding)."""
    if not is_authorized(update):
        return
    try:
        state = load_state()
        args = context.args
        if len(args) == 2:
            sym = parse_ticker(args[0])
            avg_price = parse_positive(args[1], "Average price")
        elif len(args) == 1 and len(state["positions"]) == 1:
            sym = next(iter(state["positions"]))
            avg_price = parse_positive(args[0], "Average price")
        else:
            raise ValueError("Usage: <code>/avg &lt;TICKER&gt; &lt;avg_price&gt;</code>\nExample: <code>/avg SOXL 45.12</code>")

        if sym not in state["positions"]:
            raise ValueError(f"<code>{html.escape(sym)}</code> not found. Use <code>/add {html.escape(sym)} &lt;shares&gt; {avg_price:g}</code>.")

        state["positions"][sym]["avg_price"] = avg_price
        state["positions"][sym]["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ <b>Average Updated ({html.escape(sym)})</b>\n{format_breakeven_summary(sym, state)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    try:
        if len(context.args) not in (2, 3):
            raise ValueError(
                "Usage: <code>/fees &lt;brok_%&gt; &lt;extra_%&gt; [gst_%]</code>\n"
                "• Standard: <code>/fees 0.25 0.70 18</code>\n"
                "• All-in 0.70%: <code>/fees 0 0.70 0</code>"
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
        for pos in state["positions"].values():
            pos["last_be_step"] = None
        save_state(state)

        summary = (
            f"<blockquote><b>Brokerage:</b> <code>{brok_pct:g}%</code>/side + <code>{gst_pct:g}%</code> GST\n"
            f"<b>Extra Fees:</b> <code>{extra_pct:g}%</code>\n"
            f"<i>All breakeven grids reset to recalculate on next check.</i></blockquote>"
        )
        await update.message.reply_text(
            f"✅ <b>Charge Rates Updated</b>\n{summary}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(state),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    state = load_state()
    state["alerts_enabled"] = not state.get("alerts_enabled", True)
    save_state(state)
    label = "ON 🔔" if state["alerts_enabled"] else "PAUSED 🔕"
    await update.message.reply_text(
        f"Trigger alerts are now <b>{label}</b>.",
        parse_mode="HTML",
        reply_markup=build_status_keyboard(state),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    action = query.data

    if action == "refresh":
        await query.answer("Updating prices...")
        try:
            state = load_state()
            positions = state["positions"]
            if positions:
                session = await asyncio.to_thread(get_market_session)
                quotes, errors = await asyncio.to_thread(fetch_quotes_batch, list(positions.keys()), session)
            else:
                quotes, errors = {}, {}
            await query.edit_message_text(
                format_portfolio_status(state, quotes, errors),
                parse_mode="HTML",
                reply_markup=build_status_keyboard(state),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        except Exception as e:
            await query.answer(f"Fetch failed: {e}", show_alert=True)

    elif action in ("toggle_status", "toggle_help"):
        state = load_state()
        state["alerts_enabled"] = not state.get("alerts_enabled", True)
        save_state(state)
        label = "Alerts ON 🔔" if state["alerts_enabled"] else "Alerts PAUSED 🔕"
        await query.answer(label)
        kb = build_status_keyboard(state) if action == "toggle_status" else build_help_keyboard(state)
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except BadRequest:
            pass

    elif action == "help":
        await query.answer()
        state = load_state()
        try:
            await query.edit_message_text(
                format_help(state),
                parse_mode="HTML",
                reply_markup=build_help_keyboard(state),
            )
        except BadRequest:
            pass


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    if not state.get("alerts_enabled", True) or not state["positions"]:
        return

    session = await asyncio.to_thread(get_market_session)
    if session == "closed":
        return

    now = time.monotonic()

    if session == "regular":
        if now - RUNTIME["last_regular_check"] < (REGULAR_MINUTES * 60 - 5):
            return
        RUNTIME["last_regular_check"] = now
    else:
        if now < RUNTIME["yf_cooldown_until"]:
            return
        if now - RUNTIME["last_extended_check"] < (EXTENDED_MINUTES * 60 - 5):
            return
        RUNTIME["last_extended_check"] = now

    tickers = list(state["positions"].keys())
    fetched: dict[str, tuple[float, datetime, str]] = {}

    for idx, sym in enumerate(tickers):
        if idx > 0:
            await asyncio.sleep(0.25)
        try:
            if session == "regular":
                fetched[sym] = await asyncio.to_thread(fetch_quote_for_session, sym, "regular")
            else:
                price, qdt = await asyncio.to_thread(fetch_yahoo_quote, sym)
                fetched[sym] = (price, qdt, "Yahoo Extended")
        except Exception as e:
            print(f"[{sym}] {session} check failed: {e}")
            if session != "regular" and is_rate_limit_error(e):
                RUNTIME["yf_cooldown_until"] = now + (YF_COOLDOWN_MINUTES * 60)
                print(f"Yahoo rate-limited; cooling down {YF_COOLDOWN_MINUTES}m.")
                break

    if not fetched:
        return

    # Re-read state after async network calls to avoid clobbering concurrent /sell or /toggle
    state = load_state()
    if not state.get("alerts_enabled", True):
        return

    state_dirty = False
    outbound_messages: list[str] = []

    for sym, (price, quote_dt, source) in fetched.items():
        if sym not in state["positions"]:
            continue
        if is_quote_stale(quote_dt):
            print(f"[{sym}] Skipping stale {source} quote from {quote_dt.isoformat()}.")
            continue

        pos = state["positions"][sym]
        m = calculate_metrics(price, pos, state)
        true_be = m["true_be"]
        pct_from_be = m["pct_from_be"]
        last_step = pos.get("last_be_step")

        if last_step is None:
            current_step = int(round(pct_from_be / MOVE_PCT))
            pos["last_be_step"] = current_step
            state_dirty = True
            down_step = current_step - 1
            up_step = current_step + 1
            outbound_messages.append(
                f"📌 <b>{html.escape(sym)} Grid Armed</b> <i>({html.escape(source)})</i>\n"
                f"<blockquote><b>Price:</b> <code>${price:.2f}</code> (<code>{pct_from_be:+.2f}%</code> vs <code>${true_be:.2f}</code> BE)\n"
                f"📉 <code>${step_price(true_be, down_step):.2f}</code> (<code>{step_label(down_step)}</code>)  |  "
                f"📈 <code>${step_price(true_be, up_step):.2f}</code> (<code>{step_label(up_step)}</code>)</blockquote>"
            )
            continue

        eps = 1e-9
        new_step = None
        if pct_from_be >= (last_step + 1) * MOVE_PCT - eps:
            new_step = math.floor((pct_from_be + eps) / MOVE_PCT)
        elif pct_from_be <= (last_step - 1) * MOVE_PCT + eps:
            new_step = math.ceil((pct_from_be - eps) / MOVE_PCT)

        if new_step is not None and new_step != last_step:
            last_sent = RUNTIME["last_alert_sent_at"].get(sym, 0.0)
            if (
                ALERT_COOLDOWN_MINUTES > 0
                and new_step != 0
                and (now - last_sent) < ALERT_COOLDOWN_MINUTES * 60
            ):
                continue

            direction = "up" if new_step > last_step else "down"
            pos["last_be_step"] = new_step
            state_dirty = True
            RUNTIME["last_alert_sent_at"][sym] = now

            if new_step == 0:
                header = (
                    f"🎯 <b>{html.escape(sym)} True Breakeven Reached</b> (<code>${true_be:.2f}</code>)"
                    if direction == "up"
                    else f"⚠️ <b>{html.escape(sym)} Back to True Breakeven</b> (<code>${true_be:.2f}</code>)"
                )
            elif direction == "up":
                header = f"📈 <b>{html.escape(sym)} Crossed <code>{step_label(new_step)}</code> from BE</b>"
            else:
                header = f"📉 <b>{html.escape(sym)} Dropped to <code>{step_label(new_step)}</code> from BE</b>"

            outbound_messages.append(
                format_single_card(sym, price, quote_dt, pos, state, source, compact=True, header=header)
            )

    if state_dirty:
        save_state(state)

    kb = build_status_keyboard(state)
    for text in outbound_messages:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
        )


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("status", "Live portfolio P&L & triggers"),
        BotCommand("add", "Add/set a stock (/add TQQQ 10 75.50)"),
        BotCommand("buy", "Log a buy (/buy SOXL 100 41.00)"),
        BotCommand("sell", "Remove sold stock (/sell SOXL)"),
        BotCommand("avg", "Update avg price (/avg SOXL 45.12)"),
        BotCommand("fees", "Update brokerage, extra fees & GST"),
        BotCommand("toggle", "Pause or resume price alerts"),
        BotCommand("help", "Show all commands & holdings"),
    ])


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "status"], status_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["add", "set"], add_or_set_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler(["sell", "remove"], sell_cmd))
    app.add_handler(CommandHandler("avg", avg_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("toggle", toggle_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(scheduled_check, interval=60, first=10)
    print(
        f"Portfolio hybrid bot started "
        f"(Finnhub every {REGULAR_MINUTES}m regular | Yahoo every {EXTENDED_MINUTES}m extended | "
        f"grid ±{MOVE_PCT:g}% from True BE | cooldown {ALERT_COOLDOWN_MINUTES:g}m)..."
    )
    app.run_polling()


if __name__ == "__main__":
    main()
