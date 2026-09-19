import time
import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, time as dt_time
from zoneinfo import ZoneInfo
import math
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from nordnet import NordnetClient

# ============================================================
# CONFIG
# ============================================================


# =========================
# V16 QUANT ENGINE HELPERS
# =========================
V16_ENGINE_VERSION = "V18-BEARPROOF-1.0"
V16_DEFAULT_REFRESH_SECONDS = 30
STATUS_FILE = Path("paper_data/bot_status.json")
V16_MAX_QUOTE_AGE_SECONDS = 180
V16_MIN_LIQUIDITY_SCORE = 0.15

def v16_safe_float(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def v16_confidence_score(validation_pass, signal_active, research_score=0.0,
                         spread_pct=0.0, volatility_pct=0.0, realtime=False):
    """Transparent ensemble-style confidence score; not a prediction of profit."""
    score = 0.0
    score += 35.0 if validation_pass else 0.0
    score += 25.0 if signal_active else 0.0
    score += max(-10.0, min(15.0, v16_safe_float(research_score) * 3.0))
    spread = max(0.0, v16_safe_float(spread_pct))
    vol = max(0.0, v16_safe_float(volatility_pct))
    score += max(-15.0, 10.0 - spread * 5.0)
    score += max(-10.0, 10.0 - max(0.0, vol - 60.0) / 12.0)
    score += 5.0 if realtime else 0.0
    return round(max(0.0, min(100.0, score)), 1)

def _market_session(market="US", now_utc=None):
    """Best-effort regular-session clock. Weekend/closed quotes are valid last-known data, not feed failures."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if market == "US":
        local = now_utc.astimezone(ZoneInfo("America/New_York")); start, end = dt_time(9,30), dt_time(16,0)
    else:
        local = now_utc.astimezone(ZoneInfo("Europe/Oslo")); start, end = dt_time(9,0), dt_time(16,20)
    open_now = local.weekday() < 5 and start <= local.time().replace(tzinfo=None) < end
    return {"open": open_now, "local_time": local.isoformat(), "market": market}

def v16_market_data_health(realtime=False, quote_timestamp=None, market="US"):
    now = datetime.now(timezone.utc); age = None
    if quote_timestamp is not None:
        try:
            ts = pd.to_datetime(quote_timestamp, utc=True)
            # Drop pandas nanoseconds explicitly to avoid the to_pydatetime warning.
            ts_py = ts.floor("us").to_pydatetime()
            age = max(0.0, (now - ts_py).total_seconds())
        except Exception:
            age = None
    session = _market_session(market, now)
    stale = bool(session["open"] and age is not None and age > V16_MAX_QUOTE_AGE_SECONDS)
    if not session["open"]:
        label = "MARKET CLOSED • LAST VALID QUOTE"
    elif stale:
        label = "DATA STALE • MARKET SHOULD BE OPEN"
    else:
        label = "LIVE • MARKET OPEN" if realtime else "DELAYED • MARKET OPEN"
    return {"label": label, "age_seconds": age, "stale": stale, "market_open": session["open"], "session": session}

def v16_risk_budget(equity_nok, risk_pct, drawdown_pct=0.0):
    """Reduce risk automatically as drawdown grows; never increases user-set risk."""
    equity = max(0.0, v16_safe_float(equity_nok))
    base = max(0.0, v16_safe_float(risk_pct)) / 100.0
    dd = max(0.0, v16_safe_float(drawdown_pct))
    throttle = 1.0
    if dd >= 8.0:
        throttle = 0.25
    elif dd >= 5.0:
        throttle = 0.50
    elif dd >= 3.0:
        throttle = 0.75
    return equity * base * throttle

def v16_position_size(equity_nok, cash_nok, entry_nok, stop_pct, risk_pct,
                      max_alloc_pct, fee_nok=0.0, drawdown_pct=0.0):
    entry = max(0.0, v16_safe_float(entry_nok))
    if entry <= 0:
        return 0
    stop_fraction = max(0.0001, v16_safe_float(stop_pct) / 100.0)
    risk_budget = v16_risk_budget(equity_nok, risk_pct, drawdown_pct)
    by_risk = int(risk_budget // (entry * stop_fraction))
    alloc_budget = min(max(0.0, v16_safe_float(cash_nok) - max(0.0, fee_nok)),
                       max(0.0, v16_safe_float(equity_nok)) * max(0.0, v16_safe_float(max_alloc_pct)) / 100.0)
    by_cash = int(alloc_budget // entry)
    return max(0, min(by_risk, by_cash))

def v16_expectancy(win_rate, avg_win_r, avg_loss_r):
    w = min(1.0, max(0.0, v16_safe_float(win_rate)))
    return round(w * v16_safe_float(avg_win_r) - (1.0 - w) * abs(v16_safe_float(avg_loss_r)), 4)

def v16_regime_gate(regime, allow_risk_off=False):
    r = str(regime or "").upper()
    if "RISK-OFF" in r and not allow_risk_off:
        return False, "risk-off regime"
    return True, "regime accepted"

def v16_decision_explain(symbol, validation, active, qty, data_health, confidence):
    reasons = []
    if not validation: reasons.append("held-out validation failed")
    if not active: reasons.append("no active current entry signal")
    if qty <= 0: reasons.append("position not affordable/risk-sized")
    if data_health.get("stale"): reasons.append("quote is stale")
    if confidence < 60: reasons.append(f"confidence {confidence:.1f}/100 below execution threshold")
    return "; ".join(reasons) if reasons else f"{symbol}: validation + signal + sizing + data checks passed"

def v16_append_audit(path, event):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(event)
        payload.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
        payload.setdefault("engine", V16_ENGINE_VERSION)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


STARTING_BALANCE = 2000.0

# Current test assumptions.
# Keep these configurable because brokerage pricing can change.
COMMISSION_RATE = 0.0015
MIN_COMMISSION = 29.00

# Paper FX assumptions: NOK value of 1 unit of each currency.
# Update these manually when you want the simulation to use newer FX rates.
PAPER_FX_TO_NOK = {
    "NOK": 1.0,
    "USD": 10.00,
    "EUR": 11.50,
    "SEK": 1.05,
    "DKK": 1.54,
    "GBP": 13.30,
}

EQNR_ID = 16105420
TICKER = "EQNR"
COMPANY = "Equinor"

DATA_DIR = Path("paper_data")
DATA_DIR.mkdir(exist_ok=True)

TRADES_FILE = DATA_DIR / "trades.csv"
TESTS_FILE = DATA_DIR / "tests.csv"

TRADE_COLUMNS = [
    "timestamp",
    "instrument_id",
    "ticker",
    "company",
    "currency",
    "fx_to_nok",
    "gross_value_nok",
    "side",
    "quantity",
    "price",
    "gross_value",
    "commission",
    "cash_after",
    "realized_pnl",
    "reason",
    "price_source",
]

TEST_COLUMNS = [
    "timestamp",
    "test",
    "status",
    "details",
]

st.set_page_config(
    page_title="Nordnet Paper Trader",
    page_icon="📈",
    layout="wide",
)

# ============================================================
# FILE SETUP
# ============================================================

if not TRADES_FILE.exists():
    pd.DataFrame(columns=TRADE_COLUMNS).to_csv(
        TRADES_FILE, index=False
    )

if not TESTS_FILE.exists():
    pd.DataFrame(columns=TEST_COLUMNS).to_csv(
        TESTS_FILE, index=False
    )


def read_trades():
    try:
        df = pd.read_csv(TRADES_FILE)
        for column in TRADE_COLUMNS:
            if column not in df.columns:
                df[column] = ""
        return df[TRADE_COLUMNS]
    except Exception:
        return pd.DataFrame(columns=TRADE_COLUMNS)


def read_tests():
    try:
        return pd.read_csv(TESTS_FILE)
    except Exception:
        return pd.DataFrame(columns=TEST_COLUMNS)


def log_test(name, status, details=""):
    df = read_tests()

    # Avoid filling the file with identical PASS entries.
    if not df.empty:
        existing = df[
            (df["test"] == name) &
            (df["status"] == status)
        ]

        if not existing.empty and status == "PASS":
            return

    row = pd.DataFrame([{
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "test": name,
        "status": status,
        "details": details,
    }])

    pd.concat([df, row], ignore_index=True).to_csv(
        TESTS_FILE, index=False
    )


def commission_for(value):
    return max(
        MIN_COMMISSION,
        value * COMMISSION_RATE
    )


def _indicator_last(slug):
    """Best-effort public Nordnet indicator quote. Returns None on any failure."""
    try:
        q = client.markets.indicator(slug)
        price = getattr(q, "price", None)
        value = getattr(price, "last", None) if price is not None else None
        if value is None and isinstance(q, dict):
            value = (q.get("price") or {}).get("last")
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except Exception:
        return None

@st.cache_data(ttl=300, show_spinner=False)
def live_fx_to_nok(currency):
    """Use Nordnet public FX indicators when available; fall back to explicit paper assumptions."""
    c = str(currency).upper().strip()
    if c == "NOK":
        return 1.0, "NATIVE"
    # Direct crosses are preferred. If Nordnet does not expose a cross, retain the paper fallback.
    direct = {"USD": "usdnok", "EUR": "eurnok", "SEK": "seknok", "DKK": "dkknok", "GBP": "gbpnok"}
    slug = direct.get(c)
    if slug:
        value = _indicator_last(slug)
        if value is not None:
            return value, f"NORDNET:{slug.upper()}"
    return PAPER_FX_TO_NOK.get(c), "PAPER_FALLBACK"

def fx_to_nok(currency):
    rate, _source = live_fx_to_nok(currency)
    return rate


def trade_gross_nok(quantity, price, currency):
    rate = fx_to_nok(currency)
    if rate is None or price is None:
        return None
    return float(quantity) * float(price) * rate



# ============================================================
# ALPACA US MARKET DATA
# ============================================================
ALPACA_DATA_URL = "https://data.alpaca.markets"

def alpaca_credentials():
    return (
        os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY"),
        os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY"),
    )

def alpaca_configured():
    return all(alpaca_credentials())

def alpaca_get(path, params=None):
    key, secret = alpaca_credentials()
    if not key or not secret:
        raise RuntimeError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY before starting Streamlit.")
    url = ALPACA_DATA_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
        "User-Agent": "Nordnet-Paper-Trader-V16",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body=e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Alpaca HTTP {e.code}: {body}") from e

@st.cache_data(ttl=10, show_spinner=False)
def alpaca_snapshot(symbol, feed="iex"):
    symbol=str(symbol).upper().strip()
    safe=urllib.parse.quote(symbol, safe="")
    q=(alpaca_get(f"/v2/stocks/{safe}/quotes/latest", {"feed":feed}).get("quote") or {})
    tr=(alpaca_get(f"/v2/stocks/{safe}/trades/latest", {"feed":feed}).get("trade") or {})
    return {
        "symbol":symbol, "feed":feed,
        "last":v16_safe_float(tr.get("p"), None),
        "bid":v16_safe_float(q.get("bp"), None),
        "ask":v16_safe_float(q.get("ap"), None),
        "timestamp":tr.get("t") or q.get("t"),
        "trade_exchange":tr.get("x"),
        "bid_exchange":q.get("bx"), "ask_exchange":q.get("ax"),
        "realtime":feed in ("iex","sip"),
    }

# ============================================================
# NORDNET
# ============================================================

@st.cache_resource
def get_client():
    return NordnetClient()


client = get_client()

history = None
quote = None

history_error = None
quote_error = None

# Historical data
try:
    history = client.charts.history(
        EQNR_ID,
        "YEAR_1"
    )

    history_points = list(history.price_points)

    if history_points:
        log_test(
            "EQNR historical data",
            "PASS",
            f"{len(history_points)} price points loaded"
        )

except Exception as e:
    history_error = str(e)

    log_test(
        "EQNR historical data",
        "FAIL",
        history_error
    )


# Latest public quote block
try:
    quote = client.instruments.quote(EQNR_ID)

    if quote:
        log_test(
            "EQNR quote",
            "PASS",
            f"Last={quote.last.price if quote.last else None}; "
            f"realtime={quote.realtime}"
        )

except Exception as e:
    quote_error = str(e)

    log_test(
        "EQNR quote",
        "FAIL",
        quote_error
    )


# ============================================================
# QUOTE VALUES
# ============================================================

last_price = None
bid_price = None
ask_price = None

day_open = None
day_high = None
day_low = None
day_change_pct = None

tick_time = None
realtime = False

if quote:

    if quote.last:
        last_price = float(quote.last.price)

    if quote.bid:
        bid_price = float(quote.bid.price)

    if quote.ask:
        ask_price = float(quote.ask.price)

    if quote.open:
        day_open = float(quote.open.price)

    if quote.high:
        day_high = float(quote.high.price)

    if quote.low:
        day_low = float(quote.low.price)

    day_change_pct = quote.diff_pct
    realtime = bool(quote.realtime)

    if quote.tick_timestamp:
        tick_time = pd.to_datetime(
            quote.tick_timestamp,
            unit="ms"
        )


# ============================================================
# ACCOUNT CALCULATION
# ============================================================

trades = read_trades()

cash = STARTING_BALANCE

position_qty = 0
position_cost = 0.0

realized_pnl = 0.0
total_fees = 0.0

for _, trade in trades.iterrows():

    side = str(trade["side"])
    qty = int(float(trade["quantity"]))
    price = float(trade["price"])
    fee = float(trade["commission"])

    total_fees += fee

    if side == "BUY":

        cash -= (
            qty * price
            + fee
        )

        position_qty += qty

        position_cost += (
            qty * price
            + fee
        )

    elif side == "SELL" and position_qty > 0:

        qty = min(
            qty,
            position_qty
        )

        avg_cost = (
            position_cost /
            position_qty
        )

        proceeds = (
            qty * price
            - fee
        )

        cash += proceeds

        realized_pnl += (
            proceeds
            - avg_cost * qty
        )

        position_cost -= (
            avg_cost * qty
        )

        position_qty -= qty


# ============================================================
# PORTFOLIO VALUATION
# ============================================================

# Use bid for liquidation value when available.
valuation_price = (
    bid_price
    if bid_price is not None
    else last_price
)

market_value = 0.0
unrealized_pnl = 0.0

if valuation_price is not None:

    market_value = (
        position_qty
        * valuation_price
    )

    if position_qty > 0:

        unrealized_pnl = (
            market_value
            - position_cost
        )


portfolio_value = (
    cash
    + market_value
)

net_pnl = (
    portfolio_value
    - STARTING_BALANCE
)

# ============================================================
# MARKET SCANNER
# ============================================================

@st.cache_data(ttl=60)
def load_market_scanner(sort_attribute, sort_order, limit):
    try:
        scanner_client = NordnetClient()

        return scanner_client.instruments.stocklist(
            limit=limit,
            sort_attribute=sort_attribute,
            sort_order=sort_order,
        )

    except Exception:
        return tuple()


def level_price(level):
    if level is None:
        return None

    try:
        return float(level.price)
    except Exception:
        return None


def scanner_dataframe(items):
    rows = []

    for item in items:

        price = item.price

        rows.append({
            "Instrument ID": item.instrument_id,
            "Symbol": item.symbol,
            "Company": item.name,
            "Country": item.exchange_country,
            "Exchange": ", ".join(item.exchanges or ()),
            "Currency": item.currency,
            "Last": level_price(price.last) if price else None,
            "Bid": level_price(price.bid) if price else None,
            "Ask": level_price(price.ask) if price else None,
            "Change %": price.diff_pct if price else None,
            "Spread %": price.spread_pct if price else None,
            "Volume": price.turnover_volume if price else None,
            "Turnover": price.turnover if price else None,
            "Owners": item.number_of_owners,
            "Market Cap": item.market_cap,
            "Tradable": item.is_tradable,
            "Realtime": price.realtime if price else False,
        })

    return pd.DataFrame(rows)


# ============================================================
# TECHNICAL STRATEGY ENGINE
# ============================================================

def build_indicators(price_df):
    df = price_df.copy().sort_index()
    px = pd.to_numeric(df["Price"], errors="coerce")
    df["SMA20"] = px.rolling(20).mean()
    df["SMA50"] = px.rolling(50).mean()
    delta = px.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["RSI14"] = 100 - (100 / (1 + rs))
    ema12 = px.ewm(span=12, adjust=False).mean()
    ema26 = px.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    return df

def strategy_signal(indicator_df):
    if indicator_df is None or indicator_df.empty:
        return "WAIT", 0, []
    r = indicator_df.iloc[-1]
    score = 0
    reasons = []
    if pd.notna(r.get("SMA20")) and pd.notna(r.get("SMA50")):
        if r["SMA20"] > r["SMA50"]:
            score += 1; reasons.append("SMA20 above SMA50")
        else:
            score -= 1; reasons.append("SMA20 below SMA50")
    if pd.notna(r.get("RSI14")):
        if r["RSI14"] < 35:
            score += 1; reasons.append("RSI relatively low")
        elif r["RSI14"] > 70:
            score -= 1; reasons.append("RSI relatively high")
    if pd.notna(r.get("MACD")) and pd.notna(r.get("MACD_SIGNAL")):
        if r["MACD"] > r["MACD_SIGNAL"]:
            score += 1; reasons.append("MACD above signal")
        else:
            score -= 1; reasons.append("MACD below signal")
    signal = "BUY WATCH" if score >= 2 else "SELL WATCH" if score <= -2 else "HOLD / WAIT"
    return signal, score, reasons

# ============================================================
# ADVANCED RESEARCH / BACKTEST HELPERS
# ============================================================

def add_advanced_indicators(price_df):
    df = build_indicators(price_df).copy()
    px = pd.to_numeric(df["Price"], errors="coerce")
    df["EMA20"] = px.ewm(span=20, adjust=False).mean()
    df["EMA50"] = px.ewm(span=50, adjust=False).mean()
    df["RET"] = px.pct_change()
    df["VOL20"] = df["RET"].rolling(20).std() * (252 ** 0.5)
    df["MOM20"] = px.pct_change(20)
    df["ROLL_HIGH20"] = px.rolling(20).max()
    df["ROLL_LOW20"] = px.rolling(20).min()
    df["EMA100"] = px.ewm(span=100, adjust=False).mean()
    df["MOM5"] = px.pct_change(5)
    df["MOM60"] = px.pct_change(60)
    df["VOL10"] = df["RET"].rolling(10).std() * (252 ** 0.5)
    df["DD60"] = px / px.rolling(60).max() - 1.0
    return df


def research_score(df):
    if df is None or df.empty:
        return 0, []
    r = df.iloc[-1]
    score, reasons = 0, []
    if pd.notna(r.get("EMA20")) and pd.notna(r.get("EMA50")):
        if r["EMA20"] > r["EMA50"]:
            score += 1; reasons.append("EMA20 > EMA50")
        else:
            score -= 1; reasons.append("EMA20 < EMA50")
    if pd.notna(r.get("MACD")) and pd.notna(r.get("MACD_SIGNAL")):
        if r["MACD"] > r["MACD_SIGNAL"]:
            score += 1; reasons.append("MACD bullish")
        else:
            score -= 1; reasons.append("MACD bearish")
    if pd.notna(r.get("RSI14")):
        if 45 <= r["RSI14"] <= 68:
            score += 1; reasons.append("RSI in constructive zone")
        elif r["RSI14"] >= 75:
            score -= 1; reasons.append("RSI stretched")
    if pd.notna(r.get("MOM20")):
        if r["MOM20"] > 0:
            score += 1; reasons.append("20-period momentum positive")
        else:
            score -= 1; reasons.append("20-period momentum negative")
    return score, reasons


def _strategy_positions(df, strategy_name):
    """Return long-only raw positions for several research strategies."""
    if strategy_name == "EMA + MACD":
        return ((df["EMA20"] > df["EMA50"]) & (df["MACD"] > df["MACD_SIGNAL"])).astype(int)
    if strategy_name == "Trend + Momentum":
        return ((df["EMA20"] > df["EMA50"]) & (df["MOM20"] > 0) & (df["RSI14"] < 72)).astype(int)
    if strategy_name == "SMA Trend":
        return (df["SMA20"] > df["SMA50"]).astype(int)
    if strategy_name == "MACD":
        return (df["MACD"] > df["MACD_SIGNAL"]).astype(int)
    if strategy_name == "Breakout":
        prior_high = df["Price"].rolling(20).max().shift(1)
        return ((df["Price"] > prior_high) & (df["EMA20"] > df["EMA50"])).astype(int)
    if strategy_name == "Pullback Trend":
        return ((df["EMA20"] > df["EMA50"]) & (df["RSI14"].between(38, 58)) & (df["MOM60"] > 0)).astype(int)
    if strategy_name == "Fast EMA":
        ema8 = df["Price"].ewm(span=8, adjust=False).mean()
        ema21 = df["Price"].ewm(span=21, adjust=False).mean()
        return ((ema8 > ema21) & (df["RSI14"] < 72)).astype(int)
    if strategy_name == "Momentum 60":
        return ((df["MOM20"] > 0) & (df["MOM60"] > 0) & (df["Price"] > df["EMA50"])).astype(int)
    if strategy_name == "Defensive Trend":
        return ((df["EMA20"] > df["EMA50"]) & (df["Price"] > df["EMA100"]) & (df["VOL20"] < 0.55)).astype(int)
    if strategy_name == "RSI Recovery":
        return ((df["RSI14"] > 42) & (df["RSI14"].shift(1) <= 42) & (df["Price"] > df["EMA50"])).astype(int)
    if strategy_name == "Donchian 55":
        prior_high = df["Price"].rolling(55).max().shift(1)
        return ((df["Price"] > prior_high) & (df["MOM20"] > 0)).astype(int)
    if strategy_name == "Price EMA50":
        return ((df["Price"] > df["EMA50"]) & (df["EMA20"] > df["EMA50"])).astype(int)
    if strategy_name == "Momentum 20":
        return ((df["MOM20"] > 0.04) & (df["RSI14"] < 75)).astype(int)
    if strategy_name == "Low Vol Trend":
        return ((df["EMA20"] > df["EMA50"]) & (df["MOM20"] > 0) & (df["VOL20"] < 0.40)).astype(int)
    return pd.Series(0, index=df.index, dtype=int)


def run_backtest(price_df, fee_rate=COMMISSION_RATE, min_fee=MIN_COMMISSION,
                 fx_rate=1.0, initial_cash=2000.0, strategy_name="EMA + MACD",
                 slippage_bps=5.0, stop_pct=7.0, take_profit_pct=14.0):
    """Long-only research backtest with lagged signals, costs, slippage and exits."""
    df = add_advanced_indicators(price_df).dropna(subset=["Price"]).copy()
    if len(df) < 55:
        return None, None

    raw = _strategy_positions(df, strategy_name)
    desired = raw.shift(1).fillna(0).astype(int)  # no look-ahead
    position = 0
    entry_price = None
    positions = []
    exits = []

    for i, (_, row) in enumerate(df.iterrows()):
        px = float(row["Price"])
        want = int(desired.iloc[i])
        exit_reason = ""
        if position and entry_price:
            change = px / entry_price - 1.0
            if stop_pct > 0 and change <= -(stop_pct / 100.0):
                want = 0; exit_reason = "STOP"
            elif take_profit_pct > 0 and change >= (take_profit_pct / 100.0):
                want = 0; exit_reason = "TARGET"
        if not position and want:
            entry_price = px
        elif position and not want:
            entry_price = None
        position = want
        positions.append(position)
        exits.append(exit_reason)

    df["POSITION"] = positions
    df["EXIT_REASON"] = exits
    df["STRAT_RET"] = df["RET"].fillna(0) * pd.Series(positions, index=df.index).shift(1).fillna(0)
    changes = df["POSITION"].diff().abs().fillna(df["POSITION"])
    pct_fee = max(float(fee_rate), float(min_fee) / max(float(initial_cash), 1.0))
    slip = max(float(slippage_bps), 0.0) / 2000.0
    df["COST_DRAG"] = changes * (pct_fee + slip)
    df["STRAT_RET_NET"] = df["STRAT_RET"] - df["COST_DRAG"]
    df["EQUITY"] = float(initial_cash) * (1 + df["STRAT_RET_NET"]).cumprod()
    df["BUY_HOLD"] = float(initial_cash) * (1 + df["RET"].fillna(0)).cumprod()
    peak = df["EQUITY"].cummax()
    dd = df["EQUITY"] / peak - 1
    daily = df["STRAT_RET_NET"]
    std = daily.std()
    sharpe = (daily.mean() / std * (252 ** 0.5)) if pd.notna(std) and std > 0 else 0.0
    downside = daily[daily < 0].std()
    sortino = (daily.mean() / downside * (252 ** 0.5)) if pd.notna(downside) and downside > 0 else 0.0
    total_return = df["EQUITY"].iloc[-1] / initial_cash - 1
    bh_return = df["BUY_HOLD"].iloc[-1] / initial_cash - 1
    entries = int((df["POSITION"].diff() == 1).sum() + (1 if df["POSITION"].iloc[0] == 1 else 0))
    stats = {
        "Strategy": strategy_name,
        "Strategy return %": total_return * 100,
        "Buy & hold %": bh_return * 100,
        "Max drawdown %": dd.min() * 100,
        "Sharpe approx": sharpe,
        "Sortino approx": sortino,
        "Entries": entries,
        "Ending equity": df["EQUITY"].iloc[-1],
        "Stop exits": int((df["EXIT_REASON"] == "STOP").sum()),
        "Target exits": int((df["EXIT_REASON"] == "TARGET").sum()),
    }
    return df, stats


def v18_stress_test(price_df, strategy_name, initial_cash, fx_rate, stop_pct, take_profit_pct):
    """Deterministic failure-envelope scenarios around the selected strategy."""
    scenarios = [
        ("Base", 5.0, 1.0),
        ("Slippage x2", 10.0, 1.0),
        ("Slippage x4", 20.0, 1.0),
        ("Cost + slippage shock", 30.0, 2.0),
    ]
    rows = []
    for label, slip, fee_mult in scenarios:
        _, stats = run_backtest(
            price_df, fee_rate=COMMISSION_RATE * fee_mult, min_fee=MIN_COMMISSION * fee_mult,
            initial_cash=initial_cash, fx_rate=fx_rate, strategy_name=strategy_name,
            slippage_bps=slip, stop_pct=stop_pct, take_profit_pct=take_profit_pct)
        if stats:
            rows.append({"Scenario": label, "Return %": stats["Strategy return %"],
                         "Max DD %": stats["Max drawdown %"], "Sharpe": stats["Sharpe approx"],
                         "Entries": stats["Entries"], "Ending equity": stats["Ending equity"]})
    return pd.DataFrame(rows)

def v18_capital_diagnostics(capital_nok, max_alloc_pct, risk_pct, stop_pct):
    allocation = capital_nok * max_alloc_pct / 100.0
    risk_budget = capital_nok * risk_pct / 100.0
    round_trip_min_fees = 2.0 * MIN_COMMISSION
    fee_drag_alloc = round_trip_min_fees / allocation * 100.0 if allocation > 0 else float("inf")
    return allocation, risk_budget, fee_drag_alloc

def compare_strategies(price_df, initial_cash, fx_rate=1.0, slippage_bps=5.0,
                       stop_pct=7.0, take_profit_pct=14.0):
    rows = []
    for name in ["EMA + MACD", "Trend + Momentum", "SMA Trend", "MACD"]:
        _, stats = run_backtest(price_df, initial_cash=initial_cash, fx_rate=fx_rate,
                                strategy_name=name, slippage_bps=slippage_bps,
                                stop_pct=stop_pct, take_profit_pct=take_profit_pct)
        if stats:
            rows.append(stats)
    return pd.DataFrame(rows)


def walk_forward_test(price_df, strategy_name, initial_cash, fx_rate=1.0,
                      slippage_bps=5.0, stop_pct=7.0, take_profit_pct=14.0,
                      train_fraction=0.70):
    """Simple chronological train/test split; reports both periods separately."""
    df = price_df.sort_index().copy()
    cut = max(55, int(len(df) * train_fraction))
    if len(df) - cut < 20:
        return None
    train = df.iloc[:cut]
    test = df.iloc[max(0, cut - 54):]  # indicator warm-up only
    _, train_stats = run_backtest(train, initial_cash=initial_cash, fx_rate=fx_rate,
                                  strategy_name=strategy_name, slippage_bps=slippage_bps,
                                  stop_pct=stop_pct, take_profit_pct=take_profit_pct)
    test_bt, test_stats = run_backtest(test, initial_cash=initial_cash, fx_rate=fx_rate,
                                       strategy_name=strategy_name, slippage_bps=slippage_bps,
                                       stop_pct=stop_pct, take_profit_pct=take_profit_pct)
    if not train_stats or not test_stats:
        return None
    return {"train": train_stats, "test": test_stats, "test_df": test_bt, "cut": df.index[cut]}


def risk_position_size(cash_nok, entry_nok, stop_pct, risk_pct, max_alloc_pct):
    if entry_nok <= 0 or stop_pct <= 0:
        return 0
    risk_budget = cash_nok * risk_pct / 100.0
    risk_per_share = entry_nok * stop_pct / 100.0
    by_risk = int(risk_budget // risk_per_share)
    by_alloc = int((cash_nok * max_alloc_pct / 100.0) // entry_nok)
    return max(0, min(by_risk, by_alloc))

# ============================================================
# HEADER
# ============================================================

st.title("Nordnet Paper Trader")

st.caption(
    "LOCAL PAPER-TRADING CONTROL CENTER • NO REAL ORDERS"
)

if quote:

    if realtime:
        st.success(
            "● NORDNET DATA CONNECTED • REALTIME FLAG = TRUE"
        )
    else:
        st.warning(
            "● NORDNET DATA CONNECTED • DELAYED / NON-REALTIME"
        )

else:

    st.error(
        "● NORDNET QUOTE UNAVAILABLE"
    )

    if quote_error:
        st.code(quote_error)



# ============================================================
# ALPACA US MARKET DATA PANEL
# ============================================================
st.divider()
st.subheader("US Market Data — Alpaca")
st.caption("Authenticated US data. Feed provenance is shown explicitly: IEX is live but is not full SIP/NBBO coverage.")

if alpaca_configured():
    ac1, ac2 = st.columns([2,1])
    with ac1:
        alpaca_symbol=st.text_input("US symbol", "AAPL", key="alpaca_symbol").upper().strip()
    with ac2:
        alpaca_feed=st.selectbox("US feed", ["iex","delayed_sip","sip"], key="alpaca_feed")
    try:
        aq=alpaca_snapshot(alpaca_symbol, alpaca_feed)
        health=v16_market_data_health(aq["realtime"], aq["timestamp"], "US")
        label={"iex":"LIVE IEX","sip":"LIVE SIP / ALL-US-EXCHANGES","delayed_sip":"15-MIN DELAYED SIP"}[alpaca_feed]
        if health["stale"]:
            st.error(f"● ALPACA CONNECTED • {label} • {health['label']}")
        elif health["market_open"] and aq["realtime"]:
            st.success(f"● ALPACA CONNECTED • {label} • {health['label']}")
        else:
            st.warning(f"● ALPACA CONNECTED • {label} • {health['label']}")
        x1,x2,x3,x4=st.columns(4)
        x1.metric("Last", f"${aq['last']:.2f}" if aq["last"] is not None else "N/A")
        x2.metric("Bid", f"${aq['bid']:.2f}" if aq["bid"] is not None else "N/A")
        x3.metric("Ask", f"${aq['ask']:.2f}" if aq["ask"] is not None else "N/A")
        spread=None
        if aq["bid"] is not None and aq["ask"] is not None:
            mid=(aq["bid"]+aq["ask"])/2
            spread=(aq["ask"]-aq["bid"])/mid*100 if mid else None
        x4.metric("Spread", f"{spread:.4f}%" if spread is not None else "N/A")
        st.caption(f"Timestamp: {aq['timestamp']} • Trade exchange: {aq['trade_exchange']} • Bid/ask exchanges: {aq['bid_exchange']} / {aq['ask_exchange']} • Feed: {alpaca_feed.upper()}")
    except Exception as e:
        st.error("Alpaca credentials were detected, but the market-data request failed.")
        st.code(str(e))
else:
    st.info("Alpaca integration ready. This process cannot see APCA_API_KEY_ID / APCA_API_SECRET_KEY yet; set them and restart Streamlit.")

# ============================================================
# ACCOUNT SUMMARY
# ============================================================

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "Starting Balance",
    f"{STARTING_BALANCE:.2f} NOK"
)

c2.metric(
    "Cash",
    f"{cash:.2f} NOK"
)

c3.metric(
    "Portfolio",
    f"{portfolio_value:.2f} NOK"
)

c4.metric(
    "Net P&L",
    f"{net_pnl:+.2f} NOK"
)

c5.metric(
    "Fees Paid",
    f"{total_fees:.2f} NOK"
)


# ============================================================
# QUOTE PANEL
# ============================================================

st.divider()

st.subheader(
    f"{TICKER} — {COMPANY}"
)

q1, q2, q3, q4 = st.columns(4)

q1.metric(
    "Last",
    f"{last_price:.2f} NOK"
    if last_price is not None
    else "N/A"
)

q2.metric(
    "Bid",
    f"{bid_price:.2f} NOK"
    if bid_price is not None
    else "N/A"
)

q3.metric(
    "Ask",
    f"{ask_price:.2f} NOK"
    if ask_price is not None
    else "N/A"
)

q4.metric(
    "Day Change",
    f"{day_change_pct:+.2f}%"
    if day_change_pct is not None
    else "N/A"
)


q5, q6, q7 = st.columns(3)

q5.metric(
    "Open",
    f"{day_open:.2f} NOK"
    if day_open is not None
    else "N/A"
)

q6.metric(
    "Day High",
    f"{day_high:.2f} NOK"
    if day_high is not None
    else "N/A"
)

q7.metric(
    "Day Low",
    f"{day_low:.2f} NOK"
    if day_low is not None
    else "N/A"
)


if tick_time is not None:
    st.caption(
        f"Nordnet tick timestamp: {tick_time} • "
        f"Realtime flag: {realtime}"
    )


# ============================================================
# HISTORY CHART
# ============================================================

if history:

    points = list(
        history.price_points
    )

    chart_df = pd.DataFrame([
        {
            "Date": pd.to_datetime(
                p.timestamp,
                unit="ms"
            ),
            "Price": float(p.last),
        }
        for p in points
    ])

    chart_df = chart_df.set_index(
        "Date"
    )

    st.line_chart(
        chart_df
    )

elif history_error:

    st.error(
        "Historical data unavailable"
    )

    st.code(
        history_error
    )

# ============================================================
# MARKET SCANNER UI
# ============================================================

st.divider()
st.subheader("Market Scanner")

st.caption(
    "Scans Nordnet's public stock universe. "
    "Use this to discover instruments beyond EQNR."
)

scanner_col1, scanner_col2, scanner_col3 = st.columns(3)

with scanner_col1:
    scanner_mode = st.selectbox(
        "Scan by",
        [
            "Turnover",
            "Day change — highest",
            "Day change — lowest",
        ],
    )

with scanner_col2:
    scanner_limit = st.selectbox(
        "Number of stocks",
        [10, 20, 50, 100],
        index=1,
    )

with scanner_col3:
    refresh_scanner = st.button(
        "Refresh Market Scanner",
        width="stretch",
    )

if refresh_scanner:
    load_market_scanner.clear()


if scanner_mode == "Turnover":
    scanner_sort_attribute = "turnover"
    scanner_sort_order = "desc"

elif scanner_mode == "Day change — highest":
    scanner_sort_attribute = "diff_pct"
    scanner_sort_order = "desc"

else:
    scanner_sort_attribute = "diff_pct"
    scanner_sort_order = "asc"


scanner_items = load_market_scanner(
    scanner_sort_attribute,
    scanner_sort_order,
    scanner_limit,
)

scanner_df = scanner_dataframe(
    scanner_items
)


if scanner_df.empty:

    st.warning(
        "No market scanner results were returned."
    )

else:

    st.success(
        f"Loaded {len(scanner_df)} instruments from Nordnet."
    )

    display_columns = [
        "Instrument ID",
        "Symbol",
        "Company",
        "Country",
        "Currency",
        "Last",
        "Bid",
        "Ask",
        "Change %",
        "Spread %",
        "Volume",
        "Turnover",
        "Tradable",
        "Realtime",
    ]

    available_columns = [
        column
        for column in display_columns
        if column in scanner_df.columns
    ]

    st.dataframe(
        scanner_df[available_columns],
        width="stretch",
        hide_index=True,
    )


    st.write("### Select instrument")

    instrument_options = {}

    for _, row in scanner_df.iterrows():

        instrument_id = row["Instrument ID"]

        symbol = row.get("Symbol")
        company = row.get("Company")

        label = (
            f"{symbol} — {company} "
            f"[ID {instrument_id}]"
        )

        instrument_options[label] = int(
            instrument_id
        )


    selected_label = st.selectbox(
        "Instrument",
        list(instrument_options.keys()),
        key="scanner_instrument",
    )

    selected_instrument_id = (
        instrument_options[selected_label]
    )

    selected_row = scanner_df[
        scanner_df["Instrument ID"]
        == selected_instrument_id
    ].iloc[0]


    s1, s2, s3, s4 = st.columns(4)

    selected_last = selected_row.get("Last")
    selected_change = selected_row.get("Change %")
    selected_volume = selected_row.get("Volume")
    selected_turnover = selected_row.get("Turnover")


    s1.metric(
        "Selected Last",
        f"{selected_last:.2f}"
        if pd.notna(selected_last)
        else "N/A",
    )

    s2.metric(
        "Day Change",
        f"{selected_change:+.2f}%"
        if pd.notna(selected_change)
        else "N/A",
    )

    s3.metric(
        "Volume",
        f"{selected_volume:,.0f}"
        if pd.notna(selected_volume)
        else "N/A",
    )

    s4.metric(
        "Turnover",
        f"{selected_turnover:,.0f}"
        if pd.notna(selected_turnover)
        else "N/A",
    )


    try:

        selected_history = client.charts.history(
            selected_instrument_id,
            "MONTH_3",
        )

        selected_points = list(selected_history.price_points)

        if selected_points:
            selected_chart_df = pd.DataFrame([
                {
                    "Date": pd.to_datetime(point.timestamp, unit="ms"),
                    "Price": float(point.last),
                }
                for point in selected_points
            ])

            selected_chart_df = (
                selected_chart_df
                .sort_values("Date")
                .drop_duplicates(subset=["Date"])
                .set_index("Date")
            )

            chart_last = float(selected_chart_df["Price"].iloc[-1])
            quote_last = (
                float(selected_last)
                if pd.notna(selected_last)
                else None
            )

            st.write("### Selected instrument chart")

            if quote_last is not None:
                difference = abs(chart_last - quote_last)
                tolerance = max(0.01, abs(quote_last) * 0.001)

                if difference <= tolerance:
                    st.success(
                        f"DATA CHECK PASSED • "
                        f"Quote {quote_last:.2f} = "
                        f"Chart {chart_last:.2f}"
                    )
                    log_test(
                        f"Chart integrity {selected_instrument_id}",
                        "PASS",
                        f"Quote={quote_last:.2f}, Chart={chart_last:.2f}",
                    )
                else:
                    st.error(
                        f"DATA CHECK FAILED • "
                        f"Quote {quote_last:.2f} vs "
                        f"Chart {chart_last:.2f}"
                    )
                    log_test(
                        f"Chart integrity {selected_instrument_id}",
                        "FAIL",
                        f"Quote={quote_last:.2f}, Chart={chart_last:.2f}",
                    )

            st.line_chart(selected_chart_df, height=400)

            with st.expander("Chart data diagnostics"):
                d1, d2, d3 = st.columns(3)
                d1.metric("History points", len(selected_chart_df))
                d2.metric(
                    "First close",
                    f"{selected_chart_df['Price'].iloc[0]:.2f}",
                )
                d3.metric("Last close", f"{chart_last:.2f}")

                st.write(
                    "Period:",
                    selected_chart_df.index.min(),
                    "→",
                    selected_chart_df.index.max(),
                )
        else:
            st.warning("No chart history was returned for this instrument.")

    except Exception as e:
        st.warning("Chart unavailable for this instrument.")
        st.code(str(e))

# ============================================================
# STRATEGY LAB UI
# ============================================================

st.divider()
st.subheader("Strategy Lab")
st.caption("Technical research signals only. They do not submit orders.")

if "selected_chart_df" in locals() and not selected_chart_df.empty:
    indicator_df = build_indicators(selected_chart_df)
    signal, signal_score, signal_reasons = strategy_signal(indicator_df)
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Signal", signal)
    a2.metric("Score", f"{signal_score:+d}")
    latest_ind = indicator_df.iloc[-1]
    a3.metric("RSI 14", f"{latest_ind['RSI14']:.1f}" if pd.notna(latest_ind['RSI14']) else "N/A")
    a4.metric("MACD", f"{latest_ind['MACD']:.3f}" if pd.notna(latest_ind['MACD']) else "N/A")
    st.line_chart(indicator_df[["Price", "SMA20", "SMA50"]].dropna(how="all"), height=350)
    with st.expander("Why this signal?"):
        for reason in signal_reasons:
            st.write("•", reason)
else:
    st.info("Select an instrument with chart history to calculate strategy indicators.")

# ============================================================
# RESEARCH COCKPIT + BACKTEST + RISK ENGINE
# ============================================================

st.divider()
st.subheader("Research Cockpit")
st.caption("Decision support and paper research only. Signals are not guarantees and do not submit brokerage orders.")

if "selected_chart_df" in locals() and not selected_chart_df.empty:
    adv_df = add_advanced_indicators(selected_chart_df)
    adv_score, adv_reasons = research_score(adv_df)
    last_adv = adv_df.iloc[-1]

    rc1, rc2, rc3, rc4, rc5 = st.columns(5)
    rc1.metric("Research Score", f"{adv_score:+d}")
    rc2.metric("RSI", f"{last_adv['RSI14']:.1f}" if pd.notna(last_adv.get('RSI14')) else "N/A")
    rc3.metric("20P Momentum", f"{last_adv['MOM20']*100:+.2f}%" if pd.notna(last_adv.get('MOM20')) else "N/A")
    rc4.metric("Annualized Vol", f"{last_adv['VOL20']*100:.1f}%" if pd.notna(last_adv.get('VOL20')) else "N/A")
    trend_text = "UP" if pd.notna(last_adv.get('EMA20')) and pd.notna(last_adv.get('EMA50')) and last_adv['EMA20'] > last_adv['EMA50'] else "DOWN / FLAT"
    rc5.metric("EMA Trend", trend_text)

    with st.expander("Research score details"):
        for reason in adv_reasons:
            st.write("•", reason)

    st.write("### Backtest Lab — Multi-Strategy")
    bt1, bt2, bt3 = st.columns(3)
    with bt1:
        backtest_capital = st.number_input("Backtest starting capital (NOK)", min_value=1000.0, value=2000.0, step=1000.0)
    with bt2:
        backtest_fx = fx_to_nok(str(selected_row.get("Currency") or "NOK")) if "selected_row" in locals() else 1.0
        st.metric("FX assumption", f"{backtest_fx:.4f} NOK" if backtest_fx else "N/A")
    with bt3:
        strategy_choice = st.selectbox("Strategy", ["EMA + MACD", "Trend + Momentum", "SMA Trend", "MACD"])

    bc1, bc2, bc3 = st.columns(3)
    with bc1:
        slippage_bps = st.number_input("Slippage (basis points / side)", min_value=0.0, max_value=100.0, value=5.0, step=1.0)
    with bc2:
        bt_stop_pct = st.number_input("Backtest stop-loss (%)", min_value=0.5, max_value=40.0, value=7.0, step=0.5)
    with bc3:
        bt_target_pct = st.number_input("Backtest take-profit (%)", min_value=1.0, max_value=100.0, value=14.0, step=1.0)

    bt_df, bt_stats = run_backtest(selected_chart_df, initial_cash=backtest_capital,
                                   fx_rate=backtest_fx or 1.0, strategy_name=strategy_choice,
                                   slippage_bps=slippage_bps, stop_pct=bt_stop_pct,
                                   take_profit_pct=bt_target_pct)
    if bt_stats:
        btm1, btm2, btm3, btm4, btm5, btm6 = st.columns(6)
        btm1.metric("Strategy Return", f"{bt_stats['Strategy return %']:+.2f}%")
        btm2.metric("Buy & Hold", f"{bt_stats['Buy & hold %']:+.2f}%")
        btm3.metric("Max Drawdown", f"{bt_stats['Max drawdown %']:.2f}%")
        btm4.metric("Sharpe", f"{bt_stats['Sharpe approx']:.2f}")
        btm5.metric("Sortino", f"{bt_stats['Sortino approx']:.2f}")
        btm6.metric("Entries", str(bt_stats['Entries']))
        st.line_chart(bt_df[["EQUITY", "BUY_HOLD"]], height=320)

        st.write("#### Strategy Comparison")
        comparison_df = compare_strategies(selected_chart_df, backtest_capital, backtest_fx or 1.0,
                                           slippage_bps, bt_stop_pct, bt_target_pct)
        if not comparison_df.empty:
            st.dataframe(comparison_df[["Strategy", "Strategy return %", "Max drawdown %", "Sharpe approx", "Sortino approx", "Entries"]],
                         width="stretch", hide_index=True)

        st.write("#### Chronological Train / Test Check")
        wf = walk_forward_test(selected_chart_df, strategy_choice, backtest_capital, backtest_fx or 1.0,
                               slippage_bps, bt_stop_pct, bt_target_pct)
        if wf:
            wf1, wf2, wf3, wf4 = st.columns(4)
            wf1.metric("Train return", f"{wf['train']['Strategy return %']:+.2f}%")
            wf2.metric("Test return", f"{wf['test']['Strategy return %']:+.2f}%")
            wf3.metric("Test drawdown", f"{wf['test']['Max drawdown %']:.2f}%")
            wf4.metric("Test Sharpe", f"{wf['test']['Sharpe approx']:.2f}")
            st.caption(f"Chronological split near {wf['cut']}. Test performance is kept separate to make overfitting easier to spot.")
        else:
            st.info("Not enough history for a useful chronological train/test split.")

        st.caption("Research simulation only. Includes lagged signals, configured commissions, slippage, stop/target logic and delayed/public history. It is not evidence of future profitability.")
    else:
        st.info("Not enough history for this backtest.")

    st.write("### Risk & Position Sizing")
    rr1, rr2, rr3 = st.columns(3)
    with rr1:
        risk_pct = st.number_input("Risk budget per trade (%)", min_value=0.1, max_value=10.0, value=1.0, step=0.1)
    with rr2:
        stop_pct = st.number_input("Paper stop distance (%)", min_value=0.5, max_value=30.0, value=5.0, step=0.5)
    with rr3:
        max_alloc_pct = st.number_input("Max capital allocation (%)", min_value=1.0, max_value=100.0, value=25.0, step=1.0)

    sizing_currency = str(selected_row.get("Currency") or "NOK") if "selected_row" in locals() else "NOK"
    sizing_fx = fx_to_nok(sizing_currency)
    sizing_price = float(selected_last) if "selected_last" in locals() and pd.notna(selected_last) else None
    if sizing_price is not None and sizing_fx is not None:
        entry_nok = sizing_price * sizing_fx
        suggested_qty = risk_position_size(max(paper_cash if "paper_cash" in locals() else STARTING_BALANCE, 0), entry_nok, stop_pct, risk_pct, max_alloc_pct)
        stop_nok = entry_nok * (1 - stop_pct / 100.0)
        target_2r_nok = entry_nok * (1 + 2 * stop_pct / 100.0)
        rz1, rz2, rz3, rz4 = st.columns(4)
        rz1.metric("Risk-sized shares", str(suggested_qty))
        rz2.metric("Entry / share", f"{entry_nok:.2f} NOK")
        rz3.metric("Paper stop", f"{stop_nok:.2f} NOK")
        rz4.metric("2R reference", f"{target_2r_nok:.2f} NOK")
    else:
        st.warning("Position sizing unavailable because price or paper FX is missing.")

    st.write("### Paper Auto-Decision")
    auto_threshold = st.slider("Minimum research score for BUY candidate", min_value=1, max_value=4, value=3)
    if adv_score >= auto_threshold:
        st.success(f"BUY CANDIDATE • score {adv_score:+d}. Risk checks and paper execution are still required.")
    elif adv_score <= -auto_threshold:
        st.warning(f"EXIT / AVOID CANDIDATE • score {adv_score:+d}.")
    else:
        st.info(f"NO ACTION CANDIDATE • score {adv_score:+d}.")
else:
    st.info("Research Cockpit needs a selected instrument with chart history.")

# ============================================================
# POSITION
# ============================================================

st.divider()

st.subheader(
    "Paper Position"
)

p1, p2, p3, p4 = st.columns(4)

p1.metric(
    "Shares",
    str(position_qty)
)

p2.metric(
    "Market Value",
    f"{market_value:.2f} NOK"
)

p3.metric(
    "Unrealized P&L",
    f"{unrealized_pnl:+.2f} NOK"
)

p4.metric(
    "Realized P&L",
    f"{realized_pnl:+.2f} NOK"
)


# ============================================================
# MULTI-INSTRUMENT PAPER EXECUTION ENGINE
# ============================================================

st.divider()
st.subheader("Paper Execution Engine")

st.info(
    "Simulation only. Orders below are written to the local CSV journal. "
    "No brokerage order is submitted to Nordnet."
)

trade_instrument_id = EQNR_ID
trade_ticker = TICKER
trade_company = COMPANY
trade_currency = "NOK"
trade_last = last_price
trade_bid = bid_price
trade_ask = ask_price
trade_realtime = realtime

if "selected_instrument_id" in locals():
    trade_instrument_id = int(selected_instrument_id)
    trade_ticker = str(selected_row.get("Symbol") or trade_instrument_id)
    trade_company = str(selected_row.get("Company") or trade_ticker)
    trade_currency = str(selected_row.get("Currency") or "").upper()

    try:
        selected_quote = client.instruments.quote(trade_instrument_id)
        if selected_quote:
            trade_last = level_price(selected_quote.last)
            trade_bid = level_price(selected_quote.bid)
            trade_ask = level_price(selected_quote.ask)
            trade_realtime = bool(selected_quote.realtime)
    except Exception as e:
        st.warning(f"Could not refresh selected quote: {e}")

trade_fx = fx_to_nok(trade_currency)

# Reconstruct all positions in NOK using the FX rate recorded at execution.
position_book = {}
paper_cash = STARTING_BALANCE
paper_total_fees = 0.0
paper_realized_total = 0.0

for _, trade in trades.iterrows():
    iid_raw = trade.get("instrument_id", "")
    try:
        iid = int(float(iid_raw)) if pd.notna(iid_raw) and str(iid_raw).strip() else EQNR_ID
    except Exception:
        iid = EQNR_ID

    side = str(trade.get("side", "")).upper()
    qty = int(float(trade.get("quantity", 0)))
    price = float(trade.get("price", 0))
    fee = float(trade.get("commission", 0))
    currency = str(trade.get("currency", "NOK") or "NOK").upper()

    stored_fx = trade.get("fx_to_nok", "")
    try:
        execution_fx = float(stored_fx) if pd.notna(stored_fx) and str(stored_fx).strip() else fx_to_nok(currency)
    except Exception:
        execution_fx = fx_to_nok(currency)

    stored_gross_nok = trade.get("gross_value_nok", "")
    try:
        gross_nok = (
            float(stored_gross_nok)
            if pd.notna(stored_gross_nok) and str(stored_gross_nok).strip()
            else qty * price * execution_fx
        )
    except Exception:
        gross_nok = qty * price if currency == "NOK" else 0.0

    book = position_book.setdefault(
        iid,
        {
            "ticker": str(trade.get("ticker", iid)),
            "company": str(trade.get("company", "")),
            "currency": currency,
            "qty": 0,
            "cost_nok": 0.0,
            "realized_nok": 0.0,
        },
    )

    paper_total_fees += fee

    if side == "BUY":
        paper_cash -= gross_nok + fee
        book["qty"] += qty
        book["cost_nok"] += gross_nok + fee

    elif side == "SELL" and book["qty"] > 0:
        qty = min(qty, book["qty"])
        avg_cost_nok = book["cost_nok"] / book["qty"]
        proceeds_nok = gross_nok - fee
        realized_nok = proceeds_nok - avg_cost_nok * qty

        paper_cash += proceeds_nok
        book["cost_nok"] -= avg_cost_nok * qty
        book["qty"] -= qty
        book["realized_nok"] += realized_nok
        paper_realized_total += realized_nok

selected_book = position_book.get(
    trade_instrument_id,
    {"qty": 0, "cost_nok": 0.0, "realized_nok": 0.0},
)
selected_owned_qty = int(selected_book["qty"])
selected_avg_cost_nok = (
    selected_book["cost_nok"] / selected_owned_qty
    if selected_owned_qty > 0
    else 0.0
)

e1, e2, e3, e4 = st.columns(4)
e1.metric("Trading", f"{trade_ticker} — {trade_company}")
e2.metric(
    "Last",
    f"{trade_last:.2f} {trade_currency}"
    if trade_last is not None
    else "N/A",
)
e3.metric("Owned", str(selected_owned_qty))
e4.metric(
    "Average Cost",
    f"{selected_avg_cost_nok:.2f} NOK"
    if selected_owned_qty > 0
    else "N/A",
)

if not trade_realtime:
    st.warning(
        "The selected Nordnet quote is marked delayed/non-realtime. "
        "Paper execution will use the displayed public quote."
    )

if trade_fx is None:
    st.error(
        f"No paper FX rate is configured for {trade_currency}. "
        "Trading is disabled for this currency until a rate is added."
    )
else:
    st.caption(
        f"Paper FX used for accounting: 1 {trade_currency} = "
        f"{trade_fx:.4f} NOK. This is a configurable simulation assumption, "
        "not a live FX quote."
    )

quantity = st.number_input(
    "Number of shares",
    min_value=1,
    value=1,
    step=1,
    key="paper_order_quantity",
)

trade_reason = st.text_input(
    "Journal reason / note",
    placeholder="Example: manual test, breakout test, news test...",
    key="paper_trade_reason",
)

buy_price = trade_ask if trade_ask is not None else trade_last
sell_price = trade_bid if trade_bid is not None else trade_last
buy_source = "ASK" if trade_ask is not None else "LAST"
sell_source = "BID" if trade_bid is not None else "LAST"

buy_value = quantity * buy_price if buy_price is not None else 0.0
sell_value = quantity * sell_price if sell_price is not None else 0.0

buy_gross_nok = trade_gross_nok(quantity, buy_price, trade_currency)
sell_gross_nok = trade_gross_nok(quantity, sell_price, trade_currency)

buy_fee = commission_for(buy_gross_nok) if buy_gross_nok is not None else 0.0
sell_fee = commission_for(sell_gross_nok) if sell_gross_nok is not None else 0.0

buy_total_nok = buy_gross_nok + buy_fee if buy_gross_nok is not None else None
sell_net_nok = sell_gross_nok - sell_fee if sell_gross_nok is not None else None

b1, b2 = st.columns(2)

with b1:
    st.write("### BUY")
    st.write(
        f"Execution: **{buy_price:.2f} {trade_currency} ({buy_source})**"
        if buy_price is not None
        else "Execution price unavailable"
    )
    st.write(f"Gross: **{buy_value:.2f} {trade_currency}**")
    if buy_gross_nok is not None:
        st.write(f"NOK equivalent: **{buy_gross_nok:.2f} NOK**")
        st.write(f"Commission assumption: **{buy_fee:.2f} NOK**")
        st.write(f"Total required: **{buy_total_nok:.2f} NOK**")

    can_buy = (
        buy_price is not None
        and trade_fx is not None
        and buy_total_nok is not None
        and buy_total_nok <= paper_cash
    )

    if buy_total_nok is not None and buy_total_nok > paper_cash:
        st.error(
            f"Insufficient paper cash: {paper_cash:.2f} NOK available; "
            f"{buy_total_nok:.2f} NOK required."
        )

    if st.button(
        "PAPER BUY SELECTED",
        width="stretch",
        disabled=not can_buy,
    ):
        new_cash = paper_cash - buy_total_nok
        row = pd.DataFrame([{
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "instrument_id": trade_instrument_id,
            "ticker": trade_ticker,
            "company": trade_company,
            "currency": trade_currency,
            "fx_to_nok": trade_fx,
            "gross_value_nok": buy_gross_nok,
            "side": "BUY",
            "quantity": int(quantity),
            "price": buy_price,
            "gross_value": buy_value,
            "commission": buy_fee,
            "cash_after": new_cash,
            "realized_pnl": 0.0,
            "reason": trade_reason or "Manual paper BUY",
            "price_source": buy_source,
        }])
        pd.concat([trades, row], ignore_index=True).to_csv(TRADES_FILE, index=False)
        log_test(
            f"Paper BUY {trade_ticker}",
            "PASS",
            f"{quantity} @ {buy_price:.4f} {trade_currency}; "
            f"FX={trade_fx:.4f}; gross_nok={buy_gross_nok:.2f}; "
            f"fee={buy_fee:.2f}; source={buy_source}",
        )
        st.success("Paper BUY recorded in the journal.")
        st.rerun()

with b2:
    st.write("### SELL")
    st.write(
        f"Execution: **{sell_price:.2f} {trade_currency} ({sell_source})**"
        if sell_price is not None
        else "Execution price unavailable"
    )
    st.write(f"Gross: **{sell_value:.2f} {trade_currency}**")
    if sell_gross_nok is not None:
        st.write(f"NOK equivalent: **{sell_gross_nok:.2f} NOK**")
        st.write(f"Commission assumption: **{sell_fee:.2f} NOK**")
        st.write(f"Net proceeds: **{sell_net_nok:.2f} NOK**")

    can_sell = (
        sell_price is not None
        and trade_fx is not None
        and selected_owned_qty > 0
        and quantity <= selected_owned_qty
    )

    if quantity > selected_owned_qty:
        st.error(f"Insufficient shares. You own {selected_owned_qty} {trade_ticker}.")

    if st.button(
        "PAPER SELL SELECTED",
        width="stretch",
        disabled=not can_sell,
    ):
        trade_realized_nok = sell_net_nok - selected_avg_cost_nok * quantity
        new_cash = paper_cash + sell_net_nok
        row = pd.DataFrame([{
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "instrument_id": trade_instrument_id,
            "ticker": trade_ticker,
            "company": trade_company,
            "currency": trade_currency,
            "fx_to_nok": trade_fx,
            "gross_value_nok": sell_gross_nok,
            "side": "SELL",
            "quantity": int(quantity),
            "price": sell_price,
            "gross_value": sell_value,
            "commission": sell_fee,
            "cash_after": new_cash,
            "realized_pnl": trade_realized_nok,
            "reason": trade_reason or "Manual paper SELL",
            "price_source": sell_source,
        }])
        pd.concat([trades, row], ignore_index=True).to_csv(TRADES_FILE, index=False)
        log_test(
            f"Paper SELL {trade_ticker}",
            "PASS",
            f"{quantity} @ {sell_price:.4f} {trade_currency}; "
            f"FX={trade_fx:.4f}; realized_nok={trade_realized_nok:.2f}; "
            f"source={sell_source}",
        )
        st.success("Paper SELL recorded in the journal.")
        st.rerun()

st.write("### Position Book")
position_rows = []

for iid, book in position_book.items():
    if book["qty"] <= 0 and abs(book["realized_nok"]) < 1e-12:
        continue

    current_price = None
    current_realtime = False
    try:
        pq = client.instruments.quote(iid)
        if pq:
            current_price = level_price(pq.bid) or level_price(pq.last)
            current_realtime = bool(pq.realtime)
    except Exception:
        pass

    current_fx = fx_to_nok(book["currency"])
    avg_cost_nok = book["cost_nok"] / book["qty"] if book["qty"] > 0 else 0.0
    market_value_nok = (
        book["qty"] * current_price * current_fx
        if current_price is not None and current_fx is not None
        else None
    )
    unrealized_nok = (
        market_value_nok - book["cost_nok"]
        if market_value_nok is not None
        else None
    )

    position_rows.append({
        "Instrument ID": iid,
        "Ticker": book["ticker"],
        "Company": book["company"],
        "Currency": book["currency"],
        "Shares": book["qty"],
        "Avg Cost NOK": avg_cost_nok,
        "Valuation Price": current_price,
        "FX to NOK": current_fx,
        "Market Value NOK": market_value_nok,
        "Unrealized P&L NOK": unrealized_nok,
        "Realized P&L NOK": book["realized_nok"],
        "Realtime": current_realtime,
    })

if position_rows:
    st.dataframe(pd.DataFrame(position_rows), width="stretch", hide_index=True)
else:
    st.info("No multi-instrument paper positions yet.")

st.write("### Execution Controls")
control1, control2, control3 = st.columns(3)
control1.metric("Paper Cash", f"{paper_cash:.2f} NOK")
control2.metric("Journal Fees", f"{paper_total_fees:.2f} NOK")
control3.metric("Realized P&L", f"{paper_realized_total:+.2f} NOK")

st.caption(
    "Paper executions record the native-currency price, FX-to-NOK assumption, "
    "NOK equivalent, commission, resulting NOK cash, reason, and price source."
)



# ============================================================
# PORTFOLIO RESEARCH + PAPER BOT CONTROL CENTER — V10
# ============================================================

st.divider()
st.subheader("Portfolio Research & Paper Bot — CONSOLIDATED TEST ENGINE — 2,000 NOK PAPER LAB")
st.caption("Validated-signal execution planning, risk-based sizing, exits and paper-only automation. No brokerage orders are submitted.")

V10_STRATEGIES = ["EMA + MACD", "Trend + Momentum", "SMA Trend", "MACD", "Breakout", "Pullback Trend", "Fast EMA", "Momentum 60", "Defensive Trend", "RSI Recovery", "Donchian 55", "Price EMA50", "Momentum 20", "Low Vol Trend"]


def candidate_quality(score, momentum, vol, spread, trend_ok=True, regime="UNKNOWN"):
    q = float(score) * 20.0
    if pd.notna(momentum): q += max(-20.0, min(20.0, float(momentum) * 100.0))
    if pd.notna(vol): q -= max(0.0, float(vol) * 15.0)
    if pd.notna(spread): q -= max(0.0, abs(float(spread)) * 2.0)
    if trend_ok: q += 8.0
    if regime == "RISK-OFF": q -= 15.0
    return q


def market_regime(df):
    if df is None or len(df) < 60:
        return "UNKNOWN", []
    r = df.iloc[-1]; reasons = []; bullish = 0
    if pd.notna(r.get("EMA20")) and pd.notna(r.get("EMA50")) and r["EMA20"] > r["EMA50"]:
        bullish += 1; reasons.append("EMA20 > EMA50")
    if pd.notna(r.get("MOM20")) and r["MOM20"] > 0:
        bullish += 1; reasons.append("20-period momentum positive")
    if pd.notna(r.get("DD60")) and r["DD60"] > -0.10:
        bullish += 1; reasons.append("60-period drawdown contained")
    if bullish >= 3: return "RISK-ON", reasons
    if bullish <= 1: return "RISK-OFF", reasons
    return "MIXED", reasons


def candidate_rejections(score, momentum, vol, spread, min_score, regime):
    """V13 pre-filter: reject structural problems, not ordinary market noise.

    Validation remains the hard gate. This filter is intentionally broad enough
    to let candidates reach the out-of-sample tests instead of rejecting the
    entire universe before validation.
    """
    out = []
    # A score of +2 already means multiple independent signals agree. Keep the
    # user's control, but cap the pre-filter requirement at +2; stricter score
    # preferences are still reflected in ranking/quality.
    effective_min = min(int(min_score), 2)
    if score < effective_min:
        out.append(f"score {score:+d} < {effective_min:+d}")
    # Momentum is a ranking input, not an absolute veto in V13. Trend-following
    # strategies can legitimately enter during early recoveries.
    if pd.notna(vol) and vol > 1.80:
        out.append("extreme volatility > 180%")
    if pd.notna(spread) and abs(float(spread)) > 3.0:
        out.append("spread > 3%")
    # RISK-OFF lowers candidate_quality, but does not block validation outright.
    return out


def commission_aware_max_qty(cash_nok, price_native, currency, max_alloc_pct=100.0):
    fx = fx_to_nok(currency)
    if fx is None or price_native is None or price_native <= 0 or cash_nok <= 0:
        return 0, None
    unit_nok = float(price_native) * fx
    budget = min(float(cash_nok), float(cash_nok) * float(max_alloc_pct) / 100.0)
    # Integer shares only. Fee is calculated on NOK gross value.
    rough = int(budget // unit_nok)
    for qty in range(rough, 0, -1):
        gross = qty * unit_nok
        fee = commission_for(gross)
        if gross + fee <= budget:
            return qty, {"unit_nok": unit_nok, "gross_nok": gross, "fee_nok": fee, "total_nok": gross + fee}
    return 0, {"unit_nok": unit_nok, "gross_nok": 0.0, "fee_nok": MIN_COMMISSION, "total_nok": unit_nok + MIN_COMMISSION}


def rolling_walk_forward(price_df, strategy_name, initial_cash, fx_rate=1.0, slippage_bps=5.0,
                         stop_pct=7.0, take_profit_pct=14.0, folds=3):
    df = price_df.sort_index().copy()
    if len(df) < 100: return None
    fold_size = max(20, len(df) // (folds + 1)); rows = []
    for n in range(folds):
        test_start = len(df) - (folds - n) * fold_size
        test_end = min(len(df), test_start + fold_size)
        if test_start < 55: continue
        chunk = df.iloc[max(0, test_start - 60):test_end]
        _, stats = run_backtest(chunk, initial_cash=initial_cash, fx_rate=fx_rate,
                                strategy_name=strategy_name, slippage_bps=slippage_bps,
                                stop_pct=stop_pct, take_profit_pct=take_profit_pct)
        if stats:
            rows.append({"Fold": n + 1, "Return %": stats["Strategy return %"],
                         "Sharpe": stats["Sharpe approx"], "Max DD %": stats["Max drawdown %"],
                         "Entries": stats["Entries"]})
    return pd.DataFrame(rows) if rows else None


def _validation_rank(stats):
    """Balanced research score: reward return/risk quality without selecting on return alone."""
    if not stats:
        return -1e9
    return (
        float(stats.get("Sharpe approx", 0) or 0)
        + 0.25 * float(stats.get("Sortino approx", 0) or 0)
        + float(stats.get("Strategy return %", 0) or 0) / 100.0
        + max(float(stats.get("Max drawdown %", -100) or -100), -100.0) / 500.0
    )


def candidate_parameter_search(price_df, capital_nok, fx, slippage_bps=5.0):
    """Search a compact parameter grid on TRAIN data only to reduce manual curve fitting."""
    rows = []
    stop_grid = [3.0, 5.0, 7.0, 10.0, 12.0]
    target_grid = [6.0, 10.0, 14.0, 20.0, 28.0]
    for strategy in V10_STRATEGIES:
        for stop in stop_grid:
            for target in target_grid:
                _, stats = run_backtest(
                    price_df, initial_cash=capital_nok, fx_rate=fx,
                    strategy_name=strategy, slippage_bps=slippage_bps,
                    stop_pct=stop, take_profit_pct=target,
                )
                if stats:
                    row = dict(stats)
                    row["Stop %"] = stop
                    row["Target %"] = target
                    row["Selection Score"] = _validation_rank(stats)
                    rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("Selection Score", ascending=False).reset_index(drop=True)


def robust_walk_forward(price_df, capital_nok, fx, slippage_bps=5.0, folds=3):
    """Expanding-window walk-forward: choose parameters on past data, evaluate only the next unseen fold."""
    df = price_df.sort_index().copy()
    if len(df) < 140:
        return None
    test_size = max(20, len(df) // (folds + 2))
    first_test = len(df) - folds * test_size
    rows = []
    for fold in range(folds):
        test_start = first_test + fold * test_size
        test_end = min(len(df), test_start + test_size)
        train = df.iloc[:test_start]
        test = df.iloc[max(0, test_start - 60):test_end]
        if len(train) < 80 or len(test) < 55:
            continue
        search = candidate_parameter_search(train, capital_nok, fx, slippage_bps)
        if search.empty:
            continue
        best = search.iloc[0]
        _, test_stats = run_backtest(
            test, initial_cash=capital_nok, fx_rate=fx,
            strategy_name=str(best["Strategy"]), slippage_bps=slippage_bps,
            stop_pct=float(best["Stop %"]), take_profit_pct=float(best["Target %"]),
        )
        if test_stats:
            rows.append({
                "Fold": fold + 1,
                "Strategy": str(best["Strategy"]),
                "Stop %": float(best["Stop %"]),
                "Target %": float(best["Target %"]),
                "OOS Return %": test_stats["Strategy return %"],
                "OOS Sharpe": test_stats["Sharpe approx"],
                "OOS Max DD %": test_stats["Max drawdown %"],
                "OOS Entries": test_stats["Entries"],
            })
    return pd.DataFrame(rows) if rows else None


def validate_candidate(price_df, currency, capital_nok, slippage_bps=5.0, stop_pct=7.0, target_pct=14.0):
    fx = fx_to_nok(currency)
    if fx is None or price_df is None or len(price_df) < 90:
        return {"passed": False, "reason": "missing FX or insufficient history"}

    # Hold the final 30% out. Strategy/exit parameters are selected only on the earlier 70%.
    df = price_df.sort_index().copy()
    cut = max(80, int(len(df) * 0.70))
    if len(df) - cut < 20:
        return {"passed": False, "reason": "insufficient history for held-out validation"}
    train = df.iloc[:cut]
    test = df.iloc[max(0, cut - 60):]

    search = candidate_parameter_search(train, capital_nok, fx, slippage_bps)
    if search.empty:
        return {"passed": False, "reason": "no parameter-search result"}
    best_train = search.iloc[0]
    name = str(best_train["Strategy"])
    chosen_stop = float(best_train["Stop %"])
    chosen_target = float(best_train["Target %"])

    _, full_stats = run_backtest(
        df, initial_cash=capital_nok, fx_rate=fx, strategy_name=name,
        slippage_bps=slippage_bps, stop_pct=chosen_stop, take_profit_pct=chosen_target,
    )
    test_bt, test_stats = run_backtest(
        test, initial_cash=capital_nok, fx_rate=fx, strategy_name=name,
        slippage_bps=slippage_bps, stop_pct=chosen_stop, take_profit_pct=chosen_target,
    )
    if not full_stats or not test_stats:
        return {"passed": False, "reason": "held-out backtest unavailable"}

    rwf = robust_walk_forward(df, capital_nok, fx, slippage_bps, folds=4)
    reasons = []
    passed = True
    # V13 uses a robust-but-practical gate. We still require positive unseen
    # performance; we no longer demand every noisy risk statistic be > 0.
    if full_stats["Strategy return %"] <= 0:
        passed = False; reasons.append("full-period return <= 0")
    if full_stats["Max drawdown %"] <= -25:
        passed = False; reasons.append("drawdown <= -25%")
    if int(full_stats["Entries"]) < 1:
        passed = False; reasons.append("no completed/active entries")
    if test_stats["Strategy return %"] <= 0:
        passed = False; reasons.append("OOS return <= 0")
    if test_stats["Max drawdown %"] <= -20:
        passed = False; reasons.append("OOS drawdown <= -20%")

    positive_folds = None
    if rwf is not None and len(rwf):
        positive_folds = int((rwf["OOS Return %"] > 0).sum())
        # Require a simple majority of unseen folds rather than 2/3. For four
        # folds this is 2/4, while the final 30% OOS test must also be positive.
        required = max(1, math.ceil(len(rwf) / 2))
        if positive_folds < required:
            passed = False; reasons.append(f"only {positive_folds}/{len(rwf)} positive walk-forward folds")
    else:
        passed = False; reasons.append("robust walk-forward unavailable")

    stress = v18_stress_test(df, name, capital_nok, fx, chosen_stop, chosen_target)
    if stress is None or stress.empty:
        passed = False; reasons.append("stress test unavailable")
    else:
        worst_return = float(stress["Return %"].min())
        worst_dd = float(stress["Max DD %"].min())
        if worst_dd <= -25:
            passed = False; reasons.append(f"stress drawdown {worst_dd:.1f}% <= -25%")
        if worst_return <= -10:
            passed = False; reasons.append(f"stress return {worst_return:.1f}% <= -10%")

    wf = {"train": best_train.to_dict(), "test": test_stats, "test_df": test_bt, "cut": df.index[cut]}
    return {
        "passed": passed, "strategy": name, "stop_pct": chosen_stop, "target_pct": chosen_target,
        "full": full_stats, "wf": wf, "rwf": rwf, "positive_folds": positive_folds,
        "parameter_search": search.head(10), "stress": stress,
        "reason": "; ".join(reasons) if reasons else "all held-out validation checks passed",
    }


# Guardrails are defined before candidate validation so feasibility uses the same settings.
st.write("### Paper Bot Guardrails")
g1, g2, g3, g4 = st.columns(4)
with g1: bot_min_score = st.slider("Minimum score", 1, 4, 2, key="bot_min_score_v14")
with g2: bot_risk_pct = st.number_input("Max risk / trade (%)", 0.1, 5.0, 1.0, 0.1, key="bot_risk_pct_v14")
with g3: bot_max_alloc = st.number_input("Max allocation / position (%)", 1.0, 100.0, 20.0, 1.0, key="bot_max_alloc_v14")
with g4: bot_max_positions = st.number_input("Max open positions", 1, 20, 5, 1, key="bot_max_positions_v14")

daily_loss_limit_pct = st.number_input("Paper account hard loss cap (%)", 1.0, 50.0, 10.0, 1.0, key="bot_dd_v14")

st.write("### V18 Failure Envelope / Capital Reality Check")
_alloc, _risk_budget, _fee_drag = v18_capital_diagnostics(STARTING_BALANCE, bot_max_alloc, bot_risk_pct, 7.0)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Paper capital", f"{STARTING_BALANCE:,.0f} NOK")
c2.metric("Max position budget", f"{_alloc:,.0f} NOK")
c3.metric("Risk budget / trade", f"{_risk_budget:,.0f} NOK")
c4.metric("Min-fee round trip / allocation", f"{_fee_drag:.1f}%")
if _fee_drag > 3:
    st.warning("CAPITAL CONSTRAINT: minimum commissions consume more than 3% of a max-sized round trip. The bot can still be tested, but this capital level is structurally difficult for frequent live trading under the current fee assumptions.")
elif _fee_drag > 1:
    st.warning("COST WARNING: minimum commissions are material relative to the configured position size.")
else:
    st.success("Transaction-cost burden is below 1% of the configured maximum position allocation before spread/slippage.")
st.caption("V18 treats stop-loss, take-profit, costs, slippage, held-out data and walk-forward results as part of the strategy—not optional extras.")
open_positions = sum(1 for b in position_book.values() if b.get("qty", 0) > 0) if "position_book" in locals() else 0
current_equity_est = paper_cash if "paper_cash" in locals() else STARTING_BALANCE
if "position_rows" in locals():
    current_equity_est += sum(float(r.get("Market Value NOK") or 0) for r in position_rows)
drawdown_from_start = (current_equity_est / STARTING_BALANCE - 1.0) * 100 if STARTING_BALANCE else 0.0
kill_switch = drawdown_from_start <= -daily_loss_limit_pct
if kill_switch: st.error(f"PAPER BOT LOCKED • drawdown {drawdown_from_start:.2f}% breached the kill-switch.")
elif open_positions >= bot_max_positions: st.warning(f"PAPER BOT PAUSED • {open_positions} open positions meets the configured limit.")
else: st.success(f"PAPER BOT GUARDRAILS OK • {open_positions}/{bot_max_positions} positions open.")

# V12: build a broader research universe from several independent Nordnet scanner views.
# This avoids letting one dashboard scanner sort determine the entire research opportunity set.
scan_rows = []; candidate_histories = {}
research_items = {}
for _sort, _order in [("turnover", "desc"), ("diff_pct", "desc"), ("diff_pct", "asc")]:
    try:
        for _item in load_market_scanner(_sort, _order, 100):
            research_items[int(_item.instrument_id)] = _item
    except Exception:
        pass
research_universe_df = scanner_dataframe(tuple(research_items.values())) if research_items else pd.DataFrame()
if not research_universe_df.empty:
    # Prefer names that can actually fit the paper account before expensive history/validation work,
    # but retain a diversified tail for research diagnostics.
    _pre = []
    for _, _sr in research_universe_df.iterrows():
        _px = pd.to_numeric(pd.Series([_sr.get("Ask") if pd.notna(_sr.get("Ask")) else _sr.get("Last")]), errors="coerce").iloc[0]
        _qty, _ = commission_aware_max_qty(max(paper_cash, 0), _px if pd.notna(_px) else None, str(_sr.get("Currency") or "NOK"), bot_max_alloc)
        _x = _sr.to_dict(); _x["_pre_qty"] = _qty; _pre.append(_x)
    research_universe_df = pd.DataFrame(_pre).sort_values(["_pre_qty", "Turnover"], ascending=[False, False], na_position="last") if "Turnover" in pd.DataFrame(_pre).columns else pd.DataFrame(_pre).sort_values("_pre_qty", ascending=False)
    scan_universe = research_universe_df.head(min(len(research_universe_df), 60))
    with st.spinner("V14: researching expanded Nordnet universe..."):
        for _, sr in scan_universe.iterrows():
            try:
                iid = int(sr["Instrument ID"]); h = client.charts.history(iid, "YEAR_1"); pts = list(h.price_points)
                if len(pts) < 90: continue
                pdf = pd.DataFrame({"Date": [pd.to_datetime(x.timestamp, unit="ms") for x in pts],
                                    "Price": [float(x.last) for x in pts]}).sort_values("Date").drop_duplicates("Date").set_index("Date")
                candidate_histories[iid] = pdf
                ad = add_advanced_indicators(pdf); sc, why = research_score(ad); lr = ad.iloc[-1]
                regime, regime_why = market_regime(ad)
                spread = pd.to_numeric(pd.Series([sr.get("Spread %")]), errors="coerce").iloc[0]
                trend_ok = bool(pd.notna(lr.get("EMA20")) and pd.notna(lr.get("EMA50")) and lr["EMA20"] > lr["EMA50"])
                quality = candidate_quality(sc, lr.get("MOM20"), lr.get("VOL20"), spread, trend_ok, regime)
                last_native = pd.to_numeric(pd.Series([sr.get("Ask") if pd.notna(sr.get("Ask")) else sr.get("Last")]), errors="coerce").iloc[0]
                max_qty, feas = commission_aware_max_qty(max(paper_cash, 0), last_native if pd.notna(last_native) else None,
                                                        str(sr.get("Currency") or "NOK"), bot_max_alloc)
                # Affordability bonus affects research ordering only, never validation PASS/FAIL.
                if max_qty > 0: quality += 12.0
                scan_rows.append({"Instrument ID": iid, "Symbol": sr.get("Symbol"), "Company": sr.get("Company"),
                                  "Currency": sr.get("Currency"), "Research Score": sc,
                                  "5P Momentum %": float(lr.get("MOM5"))*100 if pd.notna(lr.get("MOM5")) else None,
                                  "20P Momentum %": float(lr.get("MOM20"))*100 if pd.notna(lr.get("MOM20")) else None,
                                  "60P Momentum %": float(lr.get("MOM60"))*100 if pd.notna(lr.get("MOM60")) else None,
                                  "Volatility %": float(lr.get("VOL20"))*100 if pd.notna(lr.get("VOL20")) else None,
                                  "Spread %": spread, "Regime": regime, "Quality": quality,
                                  "Affordable Qty": max_qty, "Reasons": "; ".join(why + regime_why)})
            except Exception:
                continue

if scan_rows:
    ranked_df = pd.DataFrame(scan_rows).sort_values(["Quality", "Research Score"], ascending=False).reset_index(drop=True)
    st.write("### Multi-stock Research Ranking")
    st.dataframe(ranked_df, width="stretch", hide_index=True)
else:
    ranked_df = pd.DataFrame(); st.info("No scanner candidates had enough history for portfolio research.")

# Validate strongest eligible candidates independently. This fixes the old coupling to the manually selected stock.
st.write("### Candidate-Specific Validation — CONSOLIDATED TEST")
validation_rows = []; validation_results = {}
if not ranked_df.empty:
    for _, r in ranked_df.head(25).iterrows():
        iid = int(r["Instrument ID"]); symbol = str(r["Symbol"])
        base_rej = candidate_rejections(int(r["Research Score"]),
                                        r["20P Momentum %"] / 100 if pd.notna(r["20P Momentum %"]) else float("nan"),
                                        r["Volatility %"] / 100 if pd.notna(r["Volatility %"]) else float("nan"),
                                        r["Spread %"], bot_min_score, r["Regime"])
        if int(r.get("Affordable Qty", 0)) <= 0: base_rej.append("not affordable after allocation + commission")
        result = None
        if not base_rej:
            result = validate_candidate(candidate_histories.get(iid), str(r.get("Currency") or "NOK"),
                                        max(2000.0, STARTING_BALANCE),
                                        slippage_bps if "slippage_bps" in locals() else 5.0,
                                        bt_stop_pct if "bt_stop_pct" in locals() else 7.0,
                                        bt_target_pct if "bt_target_pct" in locals() else 14.0)
            validation_results[iid] = result
        passed = bool(result and result.get("passed"))
        validation_rows.append({"Symbol": symbol, "Candidate Filters": "PASS" if not base_rej else "FAIL",
                                "Best Strategy": result.get("strategy") if result else "—",
                                "Validation": "PASS" if passed else "FAIL",
                                "Affordable Qty": int(r.get("Affordable Qty", 0)),
                                "Why": "; ".join(base_rej) if base_rej else result.get("reason", "not tested")})
    validation_df = pd.DataFrame(validation_rows)
    st.dataframe(validation_df, width="stretch", hide_index=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Candidates tested", len(validation_df))
    c2.metric("Filter PASS", int((validation_df["Candidate Filters"] == "PASS").sum()))
    c3.metric("Validation PASS", int((validation_df["Validation"] == "PASS").sum()))
    c4.metric("Affordable", int((validation_df["Affordable Qty"] > 0).sum()))
    with st.expander("Validation methodology / anti-overfitting controls"):
        st.write("Fourteen long-only strategy families are searched across 25 stop/target combinations per strategy. V13 builds the universe from turnover, top-gainer and top-decliner Nordnet scans and uses broad structural pre-filters so ordinary volatility or a RISK-OFF label does not prevent out-of-sample testing. Parameters are selected on chronological training data only; the final 30% remains held out. Four expanding walk-forward folds independently re-select parameters from past data and test the next unseen period. Trading costs and slippage remain enabled. PASS still requires positive full-period return, positive held-out return, controlled drawdown, at least one entry, and positive results in at least half of available walk-forward folds.")
else:
    st.info("No candidates available for validation.")

validated_candidates = []
if not ranked_df.empty:
    for _, r in ranked_df.iterrows():
        iid = int(r["Instrument ID"]); vr = validation_results.get(iid)
        if vr and vr.get("passed") and int(r.get("Affordable Qty", 0)) > 0:
            validated_candidates.append((r, vr))

st.write("### Automatic Paper Decision — CONSOLIDATED TEST")
auto_enabled = st.toggle("Enable automatic PAPER decisions", value=False, key="paper_auto_toggle_v14")
auto_execute = st.toggle(
    "Auto-execute PAPER actions",
    value=False,
    key="paper_auto_execute_v14",
    help="Simulation only. When enabled, eligible V14 BUY/EXIT actions are written to the local paper-trade CSV. No Nordnet order endpoint is called.",
)
st.caption("The test engine requires BOTH candidate validation and a currently active strategy signal. All execution remains local PAPER simulation.")

def _current_strategy_signal(price_df, strategy_name):
    try:
        d = add_advanced_indicators(price_df).dropna(subset=["Price"]).copy()
        if len(d) < 60:
            return False, None
        raw = _strategy_positions(d, strategy_name)
        if raw is None or len(raw) == 0:
            return False, d
        return bool(int(raw.iloc[-1]) == 1), d
    except Exception:
        return False, None

def _paper_quote(iid, fallback_price=None):
    last = fallback_price
    bid = None
    ask = None
    realtime = False
    try:
        q = client.instruments.quote(int(iid))
        if q:
            last = level_price(q.last) if q.last is not None else last
            bid = level_price(q.bid)
            ask = level_price(q.ask)
            realtime = bool(q.realtime)
    except Exception:
        pass
    return last, bid, ask, realtime

def _risk_sized_qty(equity_nok, cash_nok, price_native, currency, stop_pct, max_alloc_pct, risk_pct):
    fx = fx_to_nok(str(currency or "NOK").upper())
    if fx is None or price_native is None or not pd.notna(price_native) or float(price_native) <= 0:
        return 0, None, None, None
    px_nok = float(price_native) * float(fx)
    stop_frac = max(float(stop_pct) / 100.0, 0.005)
    risk_budget = max(float(equity_nok), 0.0) * float(risk_pct) / 100.0
    alloc_budget = min(max(float(cash_nok), 0.0), max(float(equity_nok), 0.0) * float(max_alloc_pct) / 100.0)
    risk_qty = int(risk_budget // max(px_nok * stop_frac, 1e-9))
    alloc_qty, _ = commission_aware_max_qty(max(float(cash_nok), 0.0), float(price_native), str(currency or "NOK"), float(max_alloc_pct))
    qty = max(0, min(risk_qty, int(alloc_qty)))
    return qty, fx, risk_budget, alloc_budget

# Evaluate validated candidates against their CURRENT strategy signal.
live_candidates = []
for r, vr in validated_candidates:
    iid = int(r["Instrument ID"])
    hist = candidate_histories.get(iid)
    active, current_df = _current_strategy_signal(hist, vr["strategy"])
    fallback = None
    try:
        if current_df is not None and not current_df.empty:
            fallback = float(current_df["Price"].iloc[-1])
    except Exception:
        pass
    last_q, bid_q, ask_q, realtime_q = _paper_quote(iid, fallback)
    entry_px = ask_q if ask_q is not None else last_q
    qty, fx, risk_budget, alloc_budget = _risk_sized_qty(
        current_equity_est, paper_cash, entry_px, str(r.get("Currency") or "NOK"),
        vr.get("stop_pct", 7.0), bot_max_alloc, bot_risk_pct
    )
    live_candidates.append({
        "row": r, "vr": vr, "active": active, "last": last_q, "bid": bid_q, "ask": ask_q,
        "realtime": realtime_q, "entry_px": entry_px, "qty": qty, "fx": fx,
        "risk_budget": risk_budget, "alloc_budget": alloc_budget,
    })

# Existing positions are checked first for stop, target, trailing stop, or loss of validated signal.
exit_actions = []
for iid, book in position_book.items():
    if int(book.get("qty", 0)) <= 0:
        continue
    match = next((x for x in live_candidates if int(x["row"]["Instrument ID"]) == int(iid)), None)
    ticker = str(book.get("ticker", iid))
    qty_owned = int(book.get("qty", 0))
    avg_cost_nok = float(book.get("cost_nok", 0.0)) / qty_owned if qty_owned else 0.0
    currency = str(book.get("currency", "NOK") or "NOK")
    fx = fx_to_nok(currency)
    fallback = None
    if match:
        fallback = match["last"]
    last_q, bid_q, ask_q, realtime_q = _paper_quote(iid, fallback)
    sell_px = bid_q if bid_q is not None else last_q
    if sell_px is None or fx is None:
        continue
    mark_nok = float(sell_px) * float(fx)
    pnl_pct = (mark_nok / avg_cost_nok - 1.0) * 100.0 if avg_cost_nok > 0 else 0.0

    stop_pct = float(match["vr"].get("stop_pct", 7.0)) if match else 7.0
    target_pct = float(match["vr"].get("target_pct", 14.0)) if match else 14.0
    reason = None
    if pnl_pct <= -stop_pct:
        reason = f"stop-loss reached ({pnl_pct:+.2f}% <= -{stop_pct:.1f}%)"
    elif pnl_pct >= target_pct:
        reason = f"profit target reached ({pnl_pct:+.2f}% >= +{target_pct:.1f}%)"
    elif match and not match["active"]:
        reason = f"{match['vr']['strategy']} current signal is no longer active"

    if reason:
        exit_actions.append({
            "iid": int(iid), "ticker": ticker, "company": str(book.get("company", "")),
            "currency": currency, "qty": qty_owned, "price": float(sell_px), "fx": float(fx),
            "reason": reason, "source": "BID" if bid_q is not None else "LAST",
        })

eligible_entries = [
    x for x in live_candidates
    if x["active"] and x["qty"] > 0 and int(position_book.get(int(x["row"]["Instrument ID"]), {}).get("qty", 0)) == 0
]
eligible_entries.sort(key=lambda x: (float(x["row"]["Quality"]), int(x["row"]["Research Score"])), reverse=True)

proposed = "WAIT"
proposed_reason = "No validated candidate with an active current entry signal."
action = None

if kill_switch:
    proposed_reason = f"Paper-account kill-switch active at {drawdown_from_start:.2f}% drawdown."
elif exit_actions:
    action = ("SELL", exit_actions[0])
    proposed = f"PAPER EXIT {exit_actions[0]['ticker']}"
    proposed_reason = exit_actions[0]["reason"]
elif open_positions >= bot_max_positions:
    proposed_reason = f"Maximum open positions reached ({open_positions}/{bot_max_positions})."
elif eligible_entries:
    x = eligible_entries[0]
    r, vr = x["row"], x["vr"]
    action = ("BUY", x)
    proposed = f"PAPER BUY {r['Symbol']}"
    proposed_reason = (
        f"Validation PASS + current {vr['strategy']} signal ACTIVE • "
        f"risk-sized qty {x['qty']} • stop {vr.get('stop_pct', 0):.1f}% • "
        f"target {vr.get('target_pct', 0):.1f}%."
    )

st.metric("Bot state", proposed)
st.write(proposed_reason)

# Execution-plan panel.
if action and action[0] == "BUY":
    x = action[1]; r = x["row"]; vr = x["vr"]
    qty = int(x["qty"]); px = float(x["entry_px"]); fx = float(x["fx"])
    gross_nok = qty * px * fx
    fee = commission_for(gross_nok)
    stop_native = px * (1.0 - float(vr.get("stop_pct", 7.0)) / 100.0)
    target_native = px * (1.0 + float(vr.get("target_pct", 14.0)) / 100.0)
    risk_nok = qty * px * fx * float(vr.get("stop_pct", 7.0)) / 100.0
    a1,a2,a3,a4,a5,a6 = st.columns(6)
    a1.metric("Entry", f"{px:.4f} {r['Currency']}")
    a2.metric("Quantity", str(qty))
    a3.metric("Stop", f"{stop_native:.4f}")
    a4.metric("Target", f"{target_native:.4f}")
    a5.metric("Risk", f"{risk_nok:.2f} NOK")
    a6.metric("Est. fee", f"{fee:.2f} NOK")
    st.caption(f"Estimated allocation: {gross_nok + fee:.2f} NOK • Quote source: {'ASK' if x['ask'] is not None else 'LAST'} • realtime={x['realtime']}")
elif action and action[0] == "SELL":
    x = action[1]
    gross_nok = x["qty"] * x["price"] * x["fx"]
    fee = commission_for(gross_nok)
    a1,a2,a3,a4 = st.columns(4)
    a1.metric("Exit", f"{x['price']:.4f} {x['currency']}")
    a2.metric("Quantity", str(x["qty"]))
    a3.metric("Net proceeds", f"{gross_nok-fee:.2f} NOK")
    a4.metric("Est. fee", f"{fee:.2f} NOK")

if live_candidates:
    signal_table = []
    for x in live_candidates:
        r, vr = x["row"], x["vr"]
        signal_table.append({
            "Symbol": r["Symbol"], "Validated Strategy": vr["strategy"],
            "Current Signal": "ACTIVE" if x["active"] else "INACTIVE",
            "Risk-sized Qty": int(x["qty"]), "Stop %": float(vr.get("stop_pct", 0)),
            "Target %": float(vr.get("target_pct", 0)),
        })
    st.write("#### Validated candidates — current signal check")
    st.dataframe(pd.DataFrame(signal_table), width="stretch", hide_index=True)

# PAPER-ONLY automatic execution. Duplicate protection prevents repeated reruns from
# writing the same action again within five minutes.
executed_message = None
if auto_enabled and auto_execute and action and not kill_switch:
    side, x = action
    now = datetime.now()
    duplicate = False
    if not trades.empty and "timestamp" in trades.columns:
        try:
            recent = trades.copy()
            recent["_ts"] = pd.to_datetime(recent["timestamp"], errors="coerce")
            recent = recent[recent["_ts"] >= (pd.Timestamp(now) - pd.Timedelta(minutes=5))]
            ticker_check = str(x["row"]["Symbol"]) if side == "BUY" else str(x["ticker"])
            duplicate = bool(((recent["side"].astype(str).str.upper() == side) &
                              (recent["ticker"].astype(str) == ticker_check)).any())
        except Exception:
            duplicate = False

    if not duplicate:
        if side == "BUY":
            r, vr = x["row"], x["vr"]
            qty = int(x["qty"]); px = float(x["entry_px"]); fx = float(x["fx"])
            gross_nok = qty * px * fx; fee = commission_for(gross_nok)
            total = gross_nok + fee
            if qty > 0 and total <= paper_cash:
                row = pd.DataFrame([{
                    "timestamp": now.isoformat(timespec="seconds"),
                    "instrument_id": int(r["Instrument ID"]), "ticker": str(r["Symbol"]),
                    "company": str(r["Company"]), "currency": str(r["Currency"]),
                    "fx_to_nok": fx, "gross_value_nok": gross_nok, "side": "BUY",
                    "quantity": qty, "price": px, "gross_value": qty * px,
                    "commission": fee, "cash_after": paper_cash-total, "realized_pnl": 0.0,
                    "reason": f"TEST ENGINE AUTO PAPER: {proposed_reason}",
                    "price_source": "ASK" if x["ask"] is not None else "LAST",
                }])
                pd.concat([trades, row], ignore_index=True).to_csv(TRADES_FILE, index=False)
                executed_message = f"TEST PAPER BUY recorded: {qty} {r['Symbol']} @ {px:.4f} {r['Currency']}."
        else:
            qty = int(x["qty"]); px = float(x["price"]); fx = float(x["fx"])
            gross_nok = qty * px * fx; fee = commission_for(gross_nok)
            book = position_book.get(int(x["iid"]), {})
            avg_cost = float(book.get("cost_nok", 0.0)) / max(int(book.get("qty", 0)), 1)
            realized = gross_nok - fee - avg_cost * qty
            row = pd.DataFrame([{
                "timestamp": now.isoformat(timespec="seconds"),
                "instrument_id": int(x["iid"]), "ticker": x["ticker"], "company": x["company"],
                "currency": x["currency"], "fx_to_nok": fx, "gross_value_nok": gross_nok,
                "side": "SELL", "quantity": qty, "price": px, "gross_value": qty * px,
                "commission": fee, "cash_after": paper_cash + gross_nok-fee,
                "realized_pnl": realized, "reason": f"TEST ENGINE AUTO PAPER: {x['reason']}",
                "price_source": x["source"],
            }])
            pd.concat([trades, row], ignore_index=True).to_csv(TRADES_FILE, index=False)
            executed_message = f"TEST PAPER EXIT recorded: {qty} {x['ticker']} @ {px:.4f} {x['currency']}."

if executed_message:
    st.success(executed_message)
    st.caption("Refresh/rerun to reconstruct the portfolio from the updated paper journal.")

st.write("### Candidate Rejection Diagnostics")
if validation_rows:
    st.dataframe(pd.DataFrame(validation_rows)[["Symbol", "Candidate Filters", "Validation", "Affordable Qty", "Why"]],
                 width="stretch", hide_index=True)

st.write("### Decision Journal")
decision_file = DATA_DIR / "decisions.csv"
if st.button("Record current paper-bot decision", width="stretch", key="decision_v14"):
    action_symbol = ""
    if action:
        action_symbol = str(action[1]["row"]["Symbol"]) if action[0] == "BUY" else str(action[1]["ticker"])
    row = pd.DataFrame([{
        "timestamp": datetime.now().isoformat(timespec="seconds"), "decision": proposed,
        "symbol": action_symbol, "reason": proposed_reason, "paper_equity_nok": current_equity_est,
        "paper_cash_nok": paper_cash, "open_positions": open_positions,
        "validation_pass_count": len(validated_candidates),
        "active_signal_count": len(eligible_entries),
    }])
    if decision_file.exists():
        row = pd.concat([pd.read_csv(decision_file), row], ignore_index=True)
    row.to_csv(decision_file, index=False)
    st.success("Decision recorded.")
if decision_file.exists():
    try:
        st.dataframe(pd.read_csv(decision_file).iloc[::-1].head(50), width="stretch", hide_index=True)
    except Exception:
        pass


# ============================================================
# COST MODEL
# ============================================================

st.divider()

st.subheader(
    "Trading Cost Model"
)

st.write(
    f"""
Current simulation assumptions:

- Commission rate: **{COMMISSION_RATE * 100:.2f}%**
- Minimum commission: **{MIN_COMMISSION:.2f} NOK per transaction**
- BUY execution: **ASK**
- SELL execution: **BID**
- Starting capital: **{STARTING_BALANCE:.2f} NOK**
- Fractional shares: **disabled**
- Foreign-currency trades: **converted to NOK using the explicit PAPER_FX_TO_NOK assumptions in this file**
- FX rates: **simulation assumptions, not live FX quotes**
"""
)


# ============================================================
# TRADE JOURNAL
# ============================================================

st.divider()

st.subheader(
    "Trade Journal"
)

trades = read_trades()

if trades.empty:

    st.info(
        "No simulated trades yet."
    )

else:

    st.dataframe(
        trades.iloc[::-1],
        width="stretch"
    )


# ============================================================
# TEST JOURNAL
# ============================================================

st.divider()

st.subheader(
    "System / Test Log"
)

tests = read_tests()

if tests.empty:

    st.info(
        "No tests logged yet."
    )

else:

    st.dataframe(
        tests.iloc[::-1],
        width="stretch"
    )


# ============================================================
# RESET
# ============================================================

st.divider()

with st.expander(
    "Reset paper account"
):

    st.warning(
        "This deletes the simulated trade history "
        "and resets the account to the starting balance."
    )

    confirm_reset = st.checkbox(
        "I understand this deletes my PAPER trades"
    )

    if st.button(
        "RESET PAPER ACCOUNT",
        disabled=not confirm_reset
    ):

        pd.DataFrame(
            columns=TRADE_COLUMNS
        ).to_csv(
            TRADES_FILE,
            index=False
        )

        st.success(
            "Paper account reset."
        )

        st.rerun()


st.divider()

st.caption(
    "PAPER MODE • PUBLIC NORDNET MARKET DATA • "
    "NO BROKERAGE ORDERS ARE SUBMITTED"
)

# ============================================================
# MACHINE-READABLE STATUS + TEST HEARTBEAT
# ============================================================
try:
    status_payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "engine": V16_ENGINE_VERSION,
        "mode": "PAPER_ONLY",
        "starting_balance_nok": STARTING_BALANCE,
        "cash_nok": round(float(paper_cash), 2) if "paper_cash" in locals() else None,
        "equity_nok": round(float(current_equity_est), 2) if "current_equity_est" in locals() else None,
        "drawdown_pct": round(float(drawdown_from_start), 4) if "drawdown_from_start" in locals() else None,
        "open_positions": int(open_positions) if "open_positions" in locals() else 0,
        "kill_switch": bool(kill_switch) if "kill_switch" in locals() else False,
        "decision": proposed if "proposed" in locals() else "WAIT",
        "decision_reason": proposed_reason if "proposed_reason" in locals() else "",
        "candidates_tested": len(validation_rows) if "validation_rows" in locals() else 0,
        "validation_pass_count": len(validated_candidates) if "validated_candidates" in locals() else 0,
        "active_entry_count": len(eligible_entries) if "eligible_entries" in locals() else 0,
        "auto_decisions_enabled": bool(auto_enabled) if "auto_enabled" in locals() else False,
        "auto_execution_enabled": bool(auto_execute) if "auto_execute" in locals() else False,
        "hard_loss_cap_pct": float(daily_loss_limit_pct) if "daily_loss_limit_pct" in locals() else None,
        "risk_per_trade_pct": float(bot_risk_pct) if "bot_risk_pct" in locals() else None,
        "max_allocation_pct": float(bot_max_alloc) if "bot_max_alloc" in locals() else None,
        "min_commission_nok": MIN_COMMISSION,
        "commission_rate_pct": COMMISSION_RATE * 100.0,
        "data_boundary": "Hybrid: Alpaca authenticated US data; Nordnet public Nordic data may be delayed/non-realtime",
        "us_feed": alpaca_feed if "alpaca_feed" in locals() else None,
        "us_symbol": alpaca_symbol if "alpaca_symbol" in locals() else None,
        "us_quote": aq if "aq" in locals() else None,
        "us_market_health": health if "health" in locals() else None,
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status_payload, indent=2, ensure_ascii=False), encoding="utf-8")
except Exception as status_error:
    log_test("status heartbeat", "FAIL", str(status_error))

with st.expander("Test automation / monitoring heartbeat"):
    st.code(str(STATUS_FILE), language=None)
    st.caption("This JSON file is rewritten on every Streamlit rerun. It is designed for external monitoring without screenshots.")
    auto_refresh = st.toggle("Auto-refresh dashboard every 30 seconds", value=False, key="test_auto_refresh")
    if auto_refresh:
        import streamlit.components.v1 as components
        components.html(
            "<script>setTimeout(function(){window.parent.location.reload();},30000);</script>",
            height=0,
        )

st.caption("V18 BEARPROOF ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Signals are research outputs, not guaranteed forecasts.")
