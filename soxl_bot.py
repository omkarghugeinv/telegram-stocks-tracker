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
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["CHAT_ID"])
ADMIN_KEY = str(ADMIN_CHAT_ID)
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

DEFAULT_TICKER = os.environ.get("TICKER", "SOXL").strip().upper()
REGULAR_MINUTES = int(os.environ.get("REGULAR_MINUTES", "1"))
EXTENDED_MINUTES = int(os.environ.get("EXTENDED_MINUTES", "3"))
MOVE_PCT = float(os.environ.get("MOVE_PCT", "1.0"))
ALERT_COOLDOWN_MINUTES = float(os.environ.get("ALERT_COOLDOWN_MINUTES", "0"))
STALE_QUOTE_MINUTES = int(os.environ.get("STALE_QUOTE_MINUTES", "20"))

# Default charge settings for new portfolios (adjustable per portfolio via /fees)
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

ROLE_ALIASES = {
    "ro": "shared_ro",
    "readonly": "shared_ro",
    "read-only": "shared_ro",
    "view": "shared_ro",
    "shared-ro": "shared_ro",
    "shared_ro": "shared_ro",
    "edit": "shared_edit",
    "shared-edit": "shared_edit",
    "shared_edit": "shared_edit",
    "separate": "separate",
    "own": "separate",
    "personal": "separate",
}

ROLE_LABELS = {
    "admin": "Admin (Owner)",
    "shared_edit": "Shared • Edit",
    "shared_ro": "Shared • Read-Only",
    "separate": "Separate Portfolio",
}

YF_TICKERS: dict[str, yf.Ticker] = {}
RUNTIME = {
    "last_regular_check": 0.0,
    "last_extended_check": 0.0,
    "yf_cooldown_until": 0.0,
    "last_alert_sent_at": {},  # keyed by (portfolio_id, symbol)
    "market_status_checked_at": 0.0,
    "cached_session": "closed",
}


def make_empty_portfolio() -> dict:
    return {
        "brokerage_pct": DEFAULT_BROKERAGE_PCT,
        "gst_pct": DEFAULT_GST_PCT,
        "extra_fee_pct": DEFAULT_EXTRA_FEE_PCT,
        "alerts_enabled": True,
        "positions": {},
    }


def make_default_state() -> dict:
    admin_portfolio = make_empty_portfolio()
    admin_portfolio["positions"][DEFAULT_TICKER] = {
        "shares": 13.32899018,
        "avg_price": 45.82,
        "last_be_step": None,
    }
    return {
        "schema_version": 3,
        "users": {
            ADMIN_KEY: {
                "role": "admin",
                "portfolio_id": ADMIN_KEY,
                "label": "Admin",
            }
        },
        "portfolios": {
            ADMIN_KEY: admin_portfolio,
        },
    }


def get_yf_ticker(symbol: str) -> yf.Ticker:
    if symbol not in YF_TICKERS:
        YF_TICKERS[symbol] = yf.Ticker(symbol)
    return YF_TICKERS[symbol]


def prune_unused_caches(state: dict) -> None:
    active_pairs = set()
    active_symbols = set()
    for pid, pf in state["portfolios"].items():
        for sym in pf["positions"]:
            active_pairs.add((pid, sym))
            active_symbols.add(sym)

    for key in list(RUNTIME["last_alert_sent_at"].keys()):
        if key not in active_pairs:
            RUNTIME["last_alert_sent_at"].pop(key, None)

    for sym in list(YF_TICKERS.keys()):
        if sym not in active_symbols:
            YF_TICKERS.pop(sym, None)


def remove_position(state: dict, portfolio_id: str, sym: str) -> None:
    pf = state["portfolios"].get(portfolio_id)
    if pf and sym in pf["positions"]:
        del pf["positions"][sym]
    prune_unused_caches(state)


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


def normalize_portfolio(raw: dict) -> dict:
    pf = {
        "brokerage_pct": float(raw.get("brokerage_pct", DEFAULT_BROKERAGE_PCT)),
        "gst_pct": float(raw.get("gst_pct", DEFAULT_GST_PCT)),
        "extra_fee_pct": float(raw.get("extra_fee_pct", DEFAULT_EXTRA_FEE_PCT)),
        "alerts_enabled": bool(raw.get("alerts_enabled", True)),
        "positions": {},
    }
    for sym, pos in (raw.get("positions") or {}).items():
        if not isinstance(pos, dict):
            continue
        s = float(pos.get("shares", 0))
        a = float(pos.get("avg_price", 0))
        if s > 0 and a > 0:
            pf["positions"][sym.upper()] = {
                "shares": s,
                "avg_price": a,
                "last_be_step": pos.get("last_be_step"),
            }
    return pf


def load_state() -> dict:
    """Loads position.json and auto-migrates v1/v2 formats to multi-user v3."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)

            # Already v3 multi-user format
            if "users" in data and "portfolios" in data:
                portfolios = {
                    str(pid): normalize_portfolio(pf)
                    for pid, pf in (data.get("portfolios") or {}).items()
                    if isinstance(pf, dict)
                }
                if ADMIN_KEY not in portfolios:
                    portfolios[ADMIN_KEY] = make_empty_portfolio()

                users = {}
                for uid, u in (data.get("users") or {}).items():
                    if not isinstance(u, dict):
                        continue
                    uid_s = str(uid)
                    role = u.get("role", "shared_ro")
                    if role not in ROLE_LABELS:
                        role = "shared_ro"
                    pid = str(u.get("portfolio_id") or (uid_s if role == "separate" else ADMIN_KEY))
                    if pid not in portfolios:
                        portfolios[pid] = make_empty_portfolio()
                    users[uid_s] = {
                        "role": role,
                        "portfolio_id": pid,
                        "label": str(u.get("label") or "").strip(),
                    }

                users[ADMIN_KEY] = {
                    "role": "admin",
                    "portfolio_id": ADMIN_KEY,
                    "label": users.get(ADMIN_KEY, {}).get("label") or "Admin",
                }
                return {"schema_version": 3, "users": users, "portfolios": portfolios}

            # Migrate v2 (multi-ticker single-user) or v1 (flat single-ticker)
            if "positions" in data:
                admin_pf = normalize_portfolio(data)
            else:
                admin_pf = {
                    "brokerage_pct": float(data.get("brokerage_pct", DEFAULT_BROKERAGE_PCT)),
                    "gst_pct": float(data.get("gst_pct", DEFAULT_GST_PCT)),
                    "extra_fee_pct": float(data.get("extra_fee_pct", DEFAULT_EXTRA_FEE_PCT)),
                    "alerts_enabled": bool(data.get("alerts_enabled", True)),
                    "positions": {},
                }
                shares = float(data.get("shares", 0))
                avg_price = float(data.get("avg_price", 0))
                if shares > 0 and avg_price > 0:
                    admin_pf["positions"][DEFAULT_TICKER] = {
                        "shares": shares,
                        "avg_price": avg_price,
                        "last_be_step": data.get("last_be_step"),
                    }

            migrated = {
                "schema_version": 3,
                "users": {
                    ADMIN_KEY: {
                        "role": "admin",
                        "portfolio_id": ADMIN_KEY,
                        "label": "Admin",
                    }
                },
                "portfolios": {ADMIN_KEY: admin_pf},
            }
            save_state(migrated)
            print("Migrated position.json to multi-user v3 schema.")
            return migrated
        except Exception as e:
            print(f"Failed to read {STATE_FILE} ({e}); falling back to defaults.")

    fresh = make_default_state()
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


def can_edit(user: dict) -> bool:
    return user["role"] in ("admin", "shared_edit", "separate")


def is_admin_user(user: dict | None) -> bool:
    return bool(user and user["role"] == "admin")


def get_user_context(update: Update) -> tuple[dict, str | None, dict | None, dict | None]:
    state = load_state()
    if not update.effective_chat:
        return state, None, None, None
    uid = str(update.effective_chat.id)
    user = state["users"].get(uid)
    if not user:
        return state, uid, None, None
    portfolio = state["portfolios"].get(user["portfolio_id"])
    return state, uid, user, portfolio


def parse_ticker(raw: str) -> str:
    sym = raw.strip().lstrip("$").upper()
    if not TICKER_RE.match(sym):
        raise ValueError(f"Invalid ticker symbol: <code>{html.escape(raw)}</code>")
    return sym


def parse_chat_id(raw: str) -> int:
    try:
        val = int(raw.strip())
    except ValueError:
        raise ValueError("Chat ID must be a valid integer.")
    if val == 0:
        raise ValueError("Chat ID cannot be 0.")
    return val


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


async def fetch_quotes_batch(tickers: list[str], session: str) -> tuple[dict, dict]:
    quotes = {}
    errors = {}
    for idx, sym in enumerate(tickers):
        if idx > 0:
            await asyncio.sleep(0.25)
        try:
            quotes[sym] = await asyncio.to_thread(fetch_quote_for_session, sym, session)
        except Exception as e:
            errors[sym] = str(e)
    return quotes, errors


def is_quote_stale(quote_dt: datetime) -> bool:
    age_seconds = (datetime.now(NY_TZ) - quote_dt).total_seconds()
    return age_seconds > STALE_QUOTE_MINUTES * 60


def calculate_metrics(price: float, pos: dict, portfolio: dict) -> dict:
    shares = pos["shares"]
    avg = pos["avg_price"]
    brok_rate = portfolio["brokerage_pct"] / 100.0
    gst_mult = 1.0 + (portfolio["gst_pct"] / 100.0)
    extra_rate = portfolio["extra_fee_pct"] / 100.0

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


def build_status_keyboard(portfolio: dict, editable: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]
    if editable:
        alert_btn = "🔔 Alerts: ON" if portfolio.get("alerts_enabled", True) else "🔕 Paused"
        row.append(InlineKeyboardButton(alert_btn, callback_data="toggle_status"))
    row.append(InlineKeyboardButton("⚙️ Commands", callback_data="help"))
    return InlineKeyboardMarkup([row])


def build_help_keyboard(portfolio: dict, editable: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("⬅️ Live Status", callback_data="refresh")]
    if editable:
        alert_btn = "🔔 Alerts: ON" if portfolio.get("alerts_enabled", True) else "🔕 Paused"
        row.append(InlineKeyboardButton(alert_btn, callback_data="toggle_help"))
    return InlineKeyboardMarkup([row])


def format_single_card(
    ticker: str,
    price: float,
    quote_dt: datetime,
    pos: dict,
    portfolio: dict,
    source: str,
    compact: bool = False,
    header: str | None = None,
) -> str:
    m = calculate_metrics(price, pos, portfolio)
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


def format_portfolio_status(
    portfolio: dict,
    quotes: dict[str, tuple[float, datetime, str]],
    errors: dict[str, str],
    editable: bool,
) -> str:
    positions = portfolio["positions"]
    if not positions:
        hint = (
            "Add a stock or ETF anytime:\n• <code>/add TQQQ 10 75.50</code>\n• <code>/buy SOXL 100 41.00</code>"
            if editable
            else "The shared portfolio currently has no active holdings."
        )
        return f"📭 <b>No Active Positions</b>\n<blockquote>{hint}</blockquote>"

    if len(positions) == 1:
        sym, pos = next(iter(positions.items()))
        if sym in quotes:
            price, qdt, src = quotes[sym]
            return format_single_card(sym, price, qdt, pos, portfolio, src, compact=False)
        return f"⚠️ <b>{html.escape(sym)}</b> fetch failed: {html.escape(errors.get(sym, 'Unknown error'))}"

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

        m = calculate_metrics(price, pos, portfolio)
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


def format_breakeven_summary(ticker: str, portfolio: dict) -> str:
    pos = portfolio["positions"][ticker]
    m = calculate_metrics(pos["avg_price"], pos, portfolio)
    true_be = m["true_be"]
    return (
        f"<blockquote><b>Ticker:</b> <code>{html.escape(ticker)}</code>\n"
        f"<b>Shares:</b> <code>{pos['shares']:.8f}</code>  |  <b>Avg:</b> <code>${pos['avg_price']:.2f}</code>\n"
        f"<b>Fees:</b> <code>${m['total_charges']:.2f}</code> "
        f"(<code>{portfolio['brokerage_pct']:g}%</code> + <code>{portfolio['gst_pct']:g}%</code> GST, "
        f"<code>{portfolio['extra_fee_pct']:g}%</code> extra)\n"
        f"<b>True BE:</b> <code>${true_be:.2f}</code> "
        f"(<code>-1%</code> <code>${step_price(true_be, -1):.2f}</code> | "
        f"<code>+1%</code> <code>${step_price(true_be, 1):.2f}</code>)</blockquote>"
    )


def format_help(user: dict, portfolio: dict) -> str:
    positions = portfolio["positions"]
    if positions:
        holding_lines = []
        for sym, pos in positions.items():
            be = calculate_metrics(pos["avg_price"], pos, portfolio)["true_be"]
            holding_lines.append(
                f"• <b>{html.escape(sym)}</b>: <code>{pos['shares']:.4f}</code> sh @ "
                f"<code>${pos['avg_price']:.2f}</code> (BE <code>${be:.2f}</code>)"
            )
        holdings_block = "\n".join(holding_lines)
        sample_sym = next(iter(positions))
    else:
        holdings_block = "• <i>None (alerts idle)</i>"
        sample_sym = "SOXL"

    role_text = ROLE_LABELS.get(user["role"], user["role"])
    alerts_text = "ON 🔔" if portfolio.get("alerts_enabled", True) else "PAUSED 🔕"

    lines = [
        f"⚙️ <b>Portfolio Tracker • {html.escape(role_text)}</b>",
        (
            f"<blockquote><b>Holdings:</b>\n{holdings_block}\n"
            f"<b>Alerts:</b> {alerts_text}  |  <b>Grid:</b> <code>±{MOVE_PCT:g}%</code> from BE\n"
            f"<b>Charges:</b> <code>{portfolio['brokerage_pct']:g}%</code> + "
            f"<code>{portfolio['gst_pct']:g}%</code> GST + <code>{portfolio['extra_fee_pct']:g}%</code> extra</blockquote>"
        ),
        "<b>Commands (tap to copy):</b>",
        "• <code>/status</code> — Live portfolio P&amp;L &amp; triggers",
    ]

    if can_edit(user):
        lines.extend([
            "• <code>/add TQQQ 10 75.50</code> — Add a ticker (or overwrite)",
            f"• <code>/buy {sample_sym} 100 41.00</code> — Log a buy (USD &amp; price)",
            f"• <code>/sell {sample_sym}</code> — Remove sold ticker (no record)",
            f"• <code>/sell {sample_sym} 5</code> — Trim 5 shares only",
            f"• <code>/avg {sample_sym} 45.12</code> — Update avg price",
            f"• <code>/fees {portfolio['brokerage_pct']:g} {portfolio['extra_fee_pct']:g} {portfolio['gst_pct']:g}</code> — Update brok / extra / GST %",
            "• <code>/toggle</code> — Pause or resume alerts",
        ])

    if is_admin_user(user):
        lines.extend([
            "\n<b>Admin User Management:</b>",
            "• <code>/users</code> — List all users &amp; roles",
            "• <code>/allow 123456789 ro Rahul</code> — Shared read-only",
            "• <code>/allow 123456789 edit Rahul</code> — Shared full edit",
            "• <code>/allow 123456789 separate Rahul</code> — Separate portfolio",
            "• <code>/revoke 123456789</code> — Remove user access",
        ])

    return "\n".join(lines)


async def sync_user_commands(bot, chat_id: int, role: str | None) -> None:
    """Customizes each user's Telegram Menu button to match their permissions."""
    try:
        if role is None:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
            return

        if role == "shared_ro":
            cmds = [
                BotCommand("status", "Live shared portfolio P&L"),
                BotCommand("help", "Show holdings & access info"),
            ]
        else:
            cmds = [
                BotCommand("status", "Live portfolio P&L & triggers"),
                BotCommand("add", "Add/set a stock (/add TQQQ 10 75.50)"),
                BotCommand("buy", "Log a buy (/buy SOXL 100 41.00)"),
                BotCommand("sell", "Remove sold stock (/sell SOXL)"),
                BotCommand("avg", "Update avg price (/avg SOXL 45.12)"),
                BotCommand("fees", "Update brokerage, extra fees & GST"),
                BotCommand("toggle", "Pause or resume price alerts"),
                BotCommand("help", "Show all commands & holdings"),
            ]
            if role == "admin":
                cmds.extend([
                    BotCommand("users", "List authorized users & roles"),
                    BotCommand("allow", "Add user (/allow <id> ro|edit|separate)"),
                    BotCommand("revoke", "Remove a user (/revoke <id>)"),
                ])

        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception:
        pass


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not uid:
        return
    if not user:
        await update.message.reply_text(
            f"👋 <b>Access Required</b>\n"
            f"<blockquote>Your Telegram Chat ID: <code>{uid}</code></blockquote>\n"
            f"Send this ID to the bot admin so they can grant you shared or personal access.",
            parse_mode="HTML",
        )
        return
    await status_cmd(update, context)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not user or portfolio is None:
        return
    editable = can_edit(user)
    positions = portfolio["positions"]

    if not positions:
        await update.message.reply_text(
            format_portfolio_status(portfolio, {}, {}, editable),
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, editable),
        )
        return

    try:
        session = await asyncio.to_thread(get_market_session)
        if context.args:
            sym = parse_ticker(context.args[0])
            if sym not in positions:
                await update.message.reply_text(
                    f"<code>{html.escape(sym)}</code> is not in this portfolio.",
                    parse_mode="HTML",
                )
                return
            price, qdt, src = await asyncio.to_thread(fetch_quote_for_session, sym, session)
            await update.message.reply_text(
                format_single_card(sym, price, qdt, positions[sym], portfolio, src, compact=False),
                parse_mode="HTML",
                reply_markup=build_status_keyboard(portfolio, editable),
            )
            return

        quotes, errors = await fetch_quotes_batch(list(positions.keys()), session)
        await update.message.reply_text(
            format_portfolio_status(portfolio, quotes, errors, editable),
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, editable),
        )
    except Exception as e:
        await update.message.reply_text(f"Error fetching status: {html.escape(str(e))}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not user or portfolio is None:
        return
    await update.message.reply_text(
        format_help(user, portfolio),
        parse_mode="HTML",
        reply_markup=build_help_keyboard(portfolio, can_edit(user)),
    )


async def require_edit(update: Update, user: dict | None) -> bool:
    if not user:
        return False
    if not can_edit(user):
        await update.message.reply_text(
            "🔒 You have <b>read-only</b> access to this shared portfolio.",
            parse_mode="HTML",
        )
        return False
    return True


async def add_or_set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not await require_edit(update, user) or portfolio is None:
        return
    try:
        args = context.args
        if len(args) == 3:
            sym = parse_ticker(args[0])
            shares = parse_positive(args[1], "Shares")
            avg_price = parse_positive(args[2], "Average price")
        elif len(args) == 2 and len(portfolio["positions"]) == 1:
            sym = next(iter(portfolio["positions"]))
            shares = parse_positive(args[0], "Shares")
            avg_price = parse_positive(args[1], "Average price")
        else:
            raise ValueError(
                "Usage: <code>/add &lt;TICKER&gt; &lt;shares&gt; &lt;avg_price&gt;</code>\n"
                "Example: <code>/add TQQQ 10 75.50</code>"
            )

        is_new = sym not in portfolio["positions"]
        portfolio["positions"][sym] = {
            "shares": shares,
            "avg_price": avg_price,
            "last_be_step": None,
        }
        save_state(state)
        action = "Added" if is_new else "Updated"
        await update.message.reply_text(
            f"✅ <b>{action} {html.escape(sym)}</b>\n{format_breakeven_summary(sym, portfolio)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, True),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not await require_edit(update, user) or portfolio is None:
        return
    try:
        args = context.args
        if len(args) == 3:
            sym = parse_ticker(args[0])
            usd_spent = parse_positive(args[1], "USD spent")
            buy_price = parse_positive(args[2], "Buy price")
        elif len(args) == 2 and len(portfolio["positions"]) == 1:
            sym = next(iter(portfolio["positions"]))
            usd_spent = parse_positive(args[0], "USD spent")
            buy_price = parse_positive(args[1], "Buy price")
        else:
            raise ValueError(
                "Usage: <code>/buy &lt;TICKER&gt; &lt;usd_spent&gt; &lt;buy_price&gt;</code>\n"
                "Example: <code>/buy SOXL 100 41.00</code>"
            )

        added_shares = usd_spent / buy_price
        if sym in portfolio["positions"]:
            pos = portfolio["positions"][sym]
            old_cost = pos["shares"] * pos["avg_price"]
            new_shares = pos["shares"] + added_shares
            new_avg = (old_cost + usd_spent) / new_shares
        else:
            new_shares = added_shares
            new_avg = buy_price

        portfolio["positions"][sym] = {
            "shares": new_shares,
            "avg_price": new_avg,
            "last_be_step": None,
        }
        save_state(state)
        await update.message.reply_text(
            f"✅ <b>Bought <code>${usd_spent:.2f}</code> of {html.escape(sym)} @ <code>${buy_price:.2f}</code></b>\n"
            f"{format_breakeven_summary(sym, portfolio)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, True),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not await require_edit(update, user) or portfolio is None:
        return
    try:
        if len(context.args) not in (1, 2):
            raise ValueError(
                "Usage:\n"
                "• Sold all (remove completely): <code>/sell SOXL</code>\n"
                "• Sold partial shares: <code>/sell SOXL 5</code>"
            )
        pid = user["portfolio_id"]
        sym = parse_ticker(context.args[0])
        if sym not in portfolio["positions"]:
            raise ValueError(f"<code>{html.escape(sym)}</code> is not in this portfolio.")

        if len(context.args) == 1:
            remove_position(state, pid, sym)
            save_state(state)
            remaining = len(portfolio["positions"])
            note = "All positions cleared — polling paused until you add a ticker." if remaining == 0 else f"Tracking <b>{remaining}</b> remaining holding(s)."
            await update.message.reply_text(
                f"🗑️ <b>Removed {html.escape(sym)}</b> (no record kept).\n{note}",
                parse_mode="HTML",
                reply_markup=build_status_keyboard(portfolio, True),
            )
            return

        sold_shares = parse_positive(context.args[1], "Shares sold")
        pos = portfolio["positions"][sym]
        new_shares = pos["shares"] - sold_shares
        if new_shares <= 1e-8:
            remove_position(state, pid, sym)
            save_state(state)
            await update.message.reply_text(
                f"🗑️ <b>Removed {html.escape(sym)}</b> (position fully closed, no record kept).",
                parse_mode="HTML",
                reply_markup=build_status_keyboard(portfolio, True),
            )
            return

        pos["shares"] = new_shares
        pos["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ <b>Trimmed <code>{sold_shares:g}</code> shares of {html.escape(sym)}</b>\n"
            f"{format_breakeven_summary(sym, portfolio)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, True),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def avg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not await require_edit(update, user) or portfolio is None:
        return
    try:
        args = context.args
        if len(args) == 2:
            sym = parse_ticker(args[0])
            avg_price = parse_positive(args[1], "Average price")
        elif len(args) == 1 and len(portfolio["positions"]) == 1:
            sym = next(iter(portfolio["positions"]))
            avg_price = parse_positive(args[0], "Average price")
        else:
            raise ValueError("Usage: <code>/avg &lt;TICKER&gt; &lt;avg_price&gt;</code>\nExample: <code>/avg SOXL 45.12</code>")

        if sym not in portfolio["positions"]:
            raise ValueError(f"<code>{html.escape(sym)}</code> not found. Use <code>/add {html.escape(sym)} &lt;shares&gt; {avg_price:g}</code>.")

        portfolio["positions"][sym]["avg_price"] = avg_price
        portfolio["positions"][sym]["last_be_step"] = None
        save_state(state)
        await update.message.reply_text(
            f"✅ <b>Average Updated ({html.escape(sym)})</b>\n{format_breakeven_summary(sym, portfolio)}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, True),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not await require_edit(update, user) or portfolio is None:
        return
    try:
        if len(context.args) not in (2, 3):
            raise ValueError(
                "Usage: <code>/fees &lt;brok_%&gt; &lt;extra_%&gt; [gst_%]</code>\n"
                "• Standard: <code>/fees 0.25 0.70 18</code>\n"
                "• All-in 0.70%: <code>/fees 0 0.70 0</code>"
            )
        brok_pct = parse_pct(context.args[0], "Brokerage %", max_val=50.0)
        extra_pct = parse_pct(context.args[1], "Extra fee %", max_val=100.0)
        gst_pct = (
            parse_pct(context.args[2], "GST %", max_val=100.0, allow_max=True)
            if len(context.args) == 3
            else portfolio["gst_pct"]
        )
        if (brok_pct / 100.0) * (1.0 + gst_pct / 100.0) >= 1.0:
            raise ValueError("Combined brokerage + GST cannot be 100% or higher.")

        portfolio["brokerage_pct"] = brok_pct
        portfolio["extra_fee_pct"] = extra_pct
        portfolio["gst_pct"] = gst_pct
        for pos in portfolio["positions"].values():
            pos["last_be_step"] = None
        save_state(state)

        summary = (
            f"<blockquote><b>Brokerage:</b> <code>{brok_pct:g}%</code>/side + <code>{gst_pct:g}%</code> GST\n"
            f"<b>Extra Fees:</b> <code>{extra_pct:g}%</code>\n"
            f"<i>Breakeven grids reset to recalculate on next check.</i></blockquote>"
        )
        await update.message.reply_text(
            f"✅ <b>Charge Rates Updated</b>\n{summary}",
            parse_mode="HTML",
            reply_markup=build_status_keyboard(portfolio, True),
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    if not await require_edit(update, user) or portfolio is None:
        return
    portfolio["alerts_enabled"] = not portfolio.get("alerts_enabled", True)
    save_state(state)
    label = "ON 🔔" if portfolio["alerts_enabled"] else "PAUSED 🔕"
    await update.message.reply_text(
        f"Trigger alerts are now <b>{label}</b>.",
        parse_mode="HTML",
        reply_markup=build_status_keyboard(portfolio, True),
    )


async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /allow <chat_id> <ro|edit|separate> [name]"""
    state, uid, user, _ = get_user_context(update)
    if not is_admin_user(user):
        return
    try:
        if len(context.args) < 2:
            raise ValueError(
                "Usage: <code>/allow &lt;chat_id&gt; &lt;ro|edit|separate&gt; [name]</code>\n"
                "• Shared read-only: <code>/allow 123456789 ro Rahul</code>\n"
                "• Shared edit: <code>/allow 123456789 edit Rahul</code>\n"
                "• Separate portfolio: <code>/allow 123456789 separate Rahul</code>"
            )
        target_id = parse_chat_id(context.args[0])
        target_key = str(target_id)
        if target_key == ADMIN_KEY:
            raise ValueError("Cannot change the Admin user's role.")

        mode_raw = context.args[1].strip().lower()
        role = ROLE_ALIASES.get(mode_raw)
        if not role:
            raise ValueError("Mode must be <code>ro</code>, <code>edit</code>, or <code>separate</code>.")

        label = " ".join(context.args[2:]).strip()[:32]
        if not label:
            label = state["users"].get(target_key, {}).get("label") or f"User {target_key[-4:]}"

        if role == "separate":
            pid = target_key
            if pid not in state["portfolios"]:
                state["portfolios"][pid] = make_empty_portfolio()
        else:
            pid = ADMIN_KEY

        state["users"][target_key] = {
            "role": role,
            "portfolio_id": pid,
            "label": label,
        }
        save_state(state)
        await sync_user_commands(context.bot, target_id, role)

        # Try notifying the user (succeeds if they already tapped Start)
        notified = True
        try:
            welcome = (
                f"✅ <b>Access Granted: {html.escape(ROLE_LABELS[role])}</b>\n"
                f"Send /status to view live prices or /help for available commands."
            )
            await context.bot.send_message(chat_id=target_id, text=welcome, parse_mode="HTML")
        except (Forbidden, BadRequest):
            notified = False

        status_note = (
            "User notified."
            if notified
            else "Saved — ask them to open the bot and tap <b>Start</b> to receive alerts."
        )
        await update.message.reply_text(
            f"✅ <b>User Saved</b>\n"
            f"<blockquote><b>Name:</b> {html.escape(label)}\n"
            f"<b>Chat ID:</b> <code>{target_key}</code>\n"
            f"<b>Access:</b> {html.escape(ROLE_LABELS[role])}</blockquote>\n"
            f"<i>{status_note}</i>",
            parse_mode="HTML",
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /revoke <chat_id>"""
    state, uid, user, _ = get_user_context(update)
    if not is_admin_user(user):
        return
    try:
        if len(context.args) != 1:
            raise ValueError("Usage: <code>/revoke &lt;chat_id&gt;</code>")
        target_id = parse_chat_id(context.args[0])
        target_key = str(target_id)
        if target_key == ADMIN_KEY:
            raise ValueError("Cannot revoke Admin access.")
        if target_key not in state["users"]:
            raise ValueError(f"User <code>{target_key}</code> is not in the allowlist.")

        removed = state["users"].pop(target_key)
        pid = removed["portfolio_id"]

        # Delete separate portfolio if no other user references it
        still_used = any(u["portfolio_id"] == pid for u in state["users"].values())
        if pid != ADMIN_KEY and not still_used and pid in state["portfolios"]:
            del state["portfolios"][pid]

        prune_unused_caches(state)
        save_state(state)
        await sync_user_commands(context.bot, target_id, None)

        await update.message.reply_text(
            f"🗑️ <b>Revoked {html.escape(removed.get('label') or target_key)}</b> (<code>{target_key}</code>).",
            parse_mode="HTML",
        )
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="HTML")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: list all authorized users and roles."""
    state, uid, user, _ = get_user_context(update)
    if not is_admin_user(user):
        return

    rows = []
    for uid_s, u in state["users"].items():
        name = html.escape(u.get("label") or uid_s)
        role_txt = html.escape(ROLE_LABELS.get(u["role"], u["role"]))
        pf = state["portfolios"].get(u["portfolio_id"], {})
        count = len(pf.get("positions", {}))
        rows.append(f"• <b>{name}</b> (<code>{uid_s}</code>) — {role_txt} ({count} ticker{'s' if count != 1 else ''})")

    await update.message.reply_text(
        f"👥 <b>Authorized Users ({len(rows)})</b>\n"
        f"<blockquote>" + "\n".join(rows) + "</blockquote>\n"
        f"Add/change: <code>/allow &lt;id&gt; ro|edit|separate [name]</code>\n"
        f"Remove: <code>/revoke &lt;id&gt;</code>",
        parse_mode="HTML",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, uid, user, portfolio = get_user_context(update)
    query = update.callback_query
    if not user or portfolio is None:
        await query.answer("Access denied.", show_alert=True)
        return

    editable = can_edit(user)
    action = query.data

    if action == "refresh":
        await query.answer("Updating prices...")
        try:
            positions = portfolio["positions"]
            if positions:
                session = await asyncio.to_thread(get_market_session)
                quotes, errors = await fetch_quotes_batch(list(positions.keys()), session)
            else:
                quotes, errors = {}, {}
            await query.edit_message_text(
                format_portfolio_status(portfolio, quotes, errors, editable),
                parse_mode="HTML",
                reply_markup=build_status_keyboard(portfolio, editable),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        except Exception as e:
            await query.answer(f"Fetch failed: {e}", show_alert=True)

    elif action in ("toggle_status", "toggle_help"):
        if not editable:
            await query.answer("Read-only access — cannot toggle alerts.", show_alert=True)
            kb = build_status_keyboard(portfolio, False) if action == "toggle_status" else build_help_keyboard(portfolio, False)
            try:
                await query.edit_message_reply_markup(reply_markup=kb)
            except BadRequest:
                pass
            return

        portfolio["alerts_enabled"] = not portfolio.get("alerts_enabled", True)
        save_state(state)
        label = "Alerts ON 🔔" if portfolio["alerts_enabled"] else "Alerts PAUSED 🔕"
        await query.answer(label)
        kb = build_status_keyboard(portfolio, True) if action == "toggle_status" else build_help_keyboard(portfolio, True)
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except BadRequest:
            pass

    elif action == "help":
        await query.answer()
        try:
            await query.edit_message_text(
                format_help(user, portfolio),
                parse_mode="HTML",
                reply_markup=build_help_keyboard(portfolio, editable),
            )
        except BadRequest:
            pass


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()

    # Only poll portfolios referenced by at least one active user
    referenced_pids = {u["portfolio_id"] for u in state["users"].values()}
    active_pids = [
        pid for pid in referenced_pids
        if pid in state["portfolios"]
        and state["portfolios"][pid].get("alerts_enabled", True)
        and state["portfolios"][pid]["positions"]
    ]
    if not active_pids:
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

    # Deduplicate tickers across all active portfolios (1 API call per unique ticker)
    unique_tickers = sorted({
        sym
        for pid in active_pids
        for sym in state["portfolios"][pid]["positions"]
    })

    fetched: dict[str, tuple[float, datetime, str]] = {}
    for idx, sym in enumerate(unique_tickers):
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

    # Re-read state after async network calls to avoid overwriting concurrent user commands
    state = load_state()
    state_dirty = False
    deliveries: list[tuple[str, str]] = []  # (portfolio_id, message_html)

    for pid in active_pids:
        pf = state["portfolios"].get(pid)
        if not pf or not pf.get("alerts_enabled", True):
            continue

        for sym, (price, quote_dt, source) in fetched.items():
            if sym not in pf["positions"]:
                continue
            if is_quote_stale(quote_dt):
                print(f"[{sym}] Skipping stale {source} quote from {quote_dt.isoformat()}.")
                continue

            pos = pf["positions"][sym]
            m = calculate_metrics(price, pos, pf)
            true_be = m["true_be"]
            pct_from_be = m["pct_from_be"]
            last_step = pos.get("last_be_step")

            if last_step is None:
                current_step = int(round(pct_from_be / MOVE_PCT))
                pos["last_be_step"] = current_step
                state_dirty = True
                down_step = current_step - 1
                up_step = current_step + 1
                deliveries.append((
                    pid,
                    f"📌 <b>{html.escape(sym)} Grid Armed</b> <i>({html.escape(source)})</i>\n"
                    f"<blockquote><b>Price:</b> <code>${price:.2f}</code> (<code>{pct_from_be:+.2f}%</code> vs <code>${true_be:.2f}</code> BE)\n"
                    f"📉 <code>${step_price(true_be, down_step):.2f}</code> (<code>{step_label(down_step)}</code>)  |  "
                    f"📈 <code>${step_price(true_be, up_step):.2f}</code> (<code>{step_label(up_step)}</code>)</blockquote>",
                ))
                continue

            eps = 1e-9
            new_step = None
            if pct_from_be >= (last_step + 1) * MOVE_PCT - eps:
                new_step = math.floor((pct_from_be + eps) / MOVE_PCT)
            elif pct_from_be <= (last_step - 1) * MOVE_PCT + eps:
                new_step = math.ceil((pct_from_be - eps) / MOVE_PCT)

            if new_step is not None and new_step != last_step:
                cooldown_key = (pid, sym)
                last_sent = RUNTIME["last_alert_sent_at"].get(cooldown_key, 0.0)
                if (
                    ALERT_COOLDOWN_MINUTES > 0
                    and new_step != 0
                    and (now - last_sent) < ALERT_COOLDOWN_MINUTES * 60
                ):
                    continue

                direction = "up" if new_step > last_step else "down"
                pos["last_be_step"] = new_step
                state_dirty = True
                RUNTIME["last_alert_sent_at"][cooldown_key] = now

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

                deliveries.append((
                    pid,
                    format_single_card(sym, price, quote_dt, pos, pf, source, compact=True, header=header),
                ))

    if state_dirty:
        save_state(state)

    for pid, text in deliveries:
        pf = state["portfolios"].get(pid)
        if not pf:
            continue
        recipients = [
            (int(uid_s), u)
            for uid_s, u in state["users"].items()
            if u["portfolio_id"] == pid
        ]
        for target_chat_id, u in recipients:
            try:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=build_status_keyboard(pf, can_edit(u)),
                )
            except Exception as e:
                print(f"Failed sending alert to {target_chat_id}: {e}")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start & view your Chat ID / portfolio"),
            BotCommand("status", "Live portfolio P&L & triggers"),
            BotCommand("help", "Show available commands"),
        ],
        scope=BotCommandScopeDefault(),
    )
    state = load_state()
    for uid_s, u in state["users"].items():
        await sync_user_commands(app.bot, int(uid_s), u["role"])


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["add", "set"], add_or_set_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler(["sell", "remove"], sell_cmd))
    app.add_handler(CommandHandler("avg", avg_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("toggle", toggle_cmd))
    app.add_handler(CommandHandler(["allow", "adduser"], allow_cmd))
    app.add_handler(CommandHandler(["revoke", "deluser"], revoke_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(scheduled_check, interval=60, first=10)
    print(
        f"Multi-user portfolio bot started "
        f"(Finnhub every {REGULAR_MINUTES}m regular | Yahoo every {EXTENDED_MINUTES}m extended | "
        f"grid ±{MOVE_PCT:g}% from True BE | cooldown {ALERT_COOLDOWN_MINUTES:g}m)..."
    )
    app.run_polling()


if __name__ == "__main__":
    main()
