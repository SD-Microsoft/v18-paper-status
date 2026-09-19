from pathlib import Path
import shutil, sys, py_compile

p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dashboard.py")
text = p.read_text(encoding="utf-8")
required = 'V18.3.1-NEWSINTEL-1.31'
if required not in text:
    raise SystemExit("Required V18.3.1 engine marker not found; refusing to patch.")
backup = p.with_name("dashboard_V18_3_1_BEFORE_V18_4.py")
shutil.copy2(p, backup)

text = text.replace('V16_ENGINE_VERSION = "V18.3.1-NEWSINTEL-1.31"', 'V16_ENGINE_VERSION = "V18.4-MARKETDATA-1.4"')
text = text.replace('V16_MAX_QUOTE_AGE_SECONDS = 180', 'V16_MAX_QUOTE_AGE_SECONDS = 180\nV18_NORDIC_MAX_QUOTE_AGE_SECONDS = 180\nV18_REQUIRE_FRESH_OPEN_MARKET_QUOTES = True')

needle = '''def v16_market_data_health(realtime=False, quote_timestamp=None, market="US"):
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
'''
replacement = needle + '''
def v18_nordic_quote_health(realtime=False, quote_timestamp=None):
    """Nordic quote freshness classification for PAPER decisions."""
    now = datetime.now(timezone.utc)
    session = _market_session("NORDIC", now)
    age = None
    ts_iso = None
    if quote_timestamp is not None:
        try:
            ts = pd.to_datetime(quote_timestamp, utc=True)
            ts_py = ts.floor("us").to_pydatetime()
            age = max(0.0, (now - ts_py).total_seconds())
            ts_iso = ts_py.isoformat()
        except Exception:
            age = None
    missing_ts = quote_timestamp is None or age is None
    stale = bool(session["open"] and (missing_ts or age > V18_NORDIC_MAX_QUOTE_AGE_SECONDS))
    if not session["open"]:
        label = "MARKET CLOSED • LAST VALID QUOTE"
    elif stale and missing_ts:
        label = "QUOTE AGE UNKNOWN • MARKET OPEN"
    elif stale:
        label = "DATA STALE • MARKET OPEN"
    elif realtime:
        label = "LIVE • MARKET OPEN"
    else:
        label = "DELAYED/UNVERIFIED • MARKET OPEN"
    executable = bool((not V18_REQUIRE_FRESH_OPEN_MARKET_QUOTES) or (not session["open"]) or (not stale))
    return {
        "label": label,
        "age_seconds": age,
        "timestamp": ts_iso,
        "stale": stale,
        "market_open": session["open"],
        "realtime": bool(realtime),
        "executable": executable,
        "session": session,
    }
'''
if needle not in text:
    raise SystemExit("Could not locate market health helper.")
text = text.replace(needle, replacement, 1)

needle = '''def scanner_dataframe(items):
    rows = []

    for item in items:

        price = item.price

        rows.append({
'''
replacement = '''def scanner_dataframe(items):
    rows = []

    for item in items:

        price = item.price
        tick_raw = getattr(price, "tick_timestamp", None) if price is not None else None
        tick_iso = None
        quote_age = None
        try:
            if tick_raw is not None:
                tick_ts = pd.to_datetime(tick_raw, unit="ms", utc=True)
                tick_iso = tick_ts.isoformat()
                quote_age = max(0.0, (pd.Timestamp.now(tz="UTC") - tick_ts).total_seconds())
        except Exception:
            pass

        rows.append({
'''
if needle not in text:
    raise SystemExit("Could not locate scanner_dataframe.")
text = text.replace(needle, replacement, 1)

needle = '''            "Tradable": item.is_tradable,
            "Realtime": price.realtime if price else False,
        })
'''
replacement = '''            "Tradable": item.is_tradable,
            "Realtime": price.realtime if price else False,
            "Quote Timestamp": tick_iso,
            "Quote Age Seconds": quote_age,
        })
'''
if needle not in text:
    raise SystemExit("Could not extend scanner quote metadata.")
text = text.replace(needle, replacement, 1)

needle = '''def _paper_quote(iid, fallback_price=None):
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
'''
replacement = '''def _paper_quote(iid, fallback_price=None):
    last = fallback_price
    bid = None
    ask = None
    realtime = False
    tick_timestamp = None
    try:
        q = client.instruments.quote(int(iid))
        if q:
            last = level_price(q.last) if q.last is not None else last
            bid = level_price(q.bid)
            ask = level_price(q.ask)
            realtime = bool(q.realtime)
            raw_tick = getattr(q, "tick_timestamp", None)
            if raw_tick is not None:
                try:
                    tick_timestamp = pd.to_datetime(raw_tick, unit="ms", utc=True).isoformat()
                except Exception:
                    tick_timestamp = None
    except Exception:
        pass
    health = v18_nordic_quote_health(realtime, tick_timestamp)
    return last, bid, ask, realtime, tick_timestamp, health
'''
if needle not in text:
    raise SystemExit("Could not locate _paper_quote.")
text = text.replace(needle, replacement, 1)

text = text.replace(
    'last_q, bid_q, ask_q, realtime_q = _paper_quote(iid, fallback)\n    entry_px = ask_q if ask_q is not None else last_q',
    'last_q, bid_q, ask_q, realtime_q, tick_q, health_q = _paper_quote(iid, fallback)\n    entry_px = ask_q if ask_q is not None else last_q'
)
text = text.replace(
    '"realtime": realtime_q, "entry_px": entry_px, "qty": qty, "fx": fx,',
    '"realtime": realtime_q, "quote_timestamp": tick_q, "data_health": health_q, "entry_px": entry_px, "qty": qty, "fx": fx,'
)
text = text.replace(
    'last_q, bid_q, ask_q, realtime_q = _paper_quote(iid, fallback)\n    sell_px = bid_q if bid_q is not None else last_q',
    'last_q, bid_q, ask_q, realtime_q, tick_q, health_q = _paper_quote(iid, fallback)\n    sell_px = bid_q if bid_q is not None else last_q'
)
text = text.replace(
    'if sell_px is None or fx is None:\n        continue',
    'if sell_px is None or fx is None:\n        continue\n    if health_q.get("market_open") and health_q.get("stale"):\n        continue',
    1
)
text = text.replace(
    'if x["active"] and x["qty"] > 0 and int(position_book.get(int(x["row"]["Instrument ID"]), {}).get("qty", 0)) == 0',
    'if x["active"] and x["qty"] > 0 and (not x["data_health"].get("market_open") or not x["data_health"].get("stale")) and int(position_book.get(int(x["row"]["Instrument ID"]), {}).get("qty", 0)) == 0'
)

text = text.replace(
    'st.caption(f"Estimated allocation: {gross_nok + fee:.2f} NOK • Quote source: {\'ASK\' if x[\'ask\'] is not None else \'LAST\'} • realtime={x[\'realtime\']}")',
    'st.caption(f"Estimated allocation: {gross_nok + fee:.2f} NOK • Quote source: {\'ASK\' if x[\'ask\'] is not None else \'LAST\'} • {x[\'data_health\'].get(\'label\')} • age={x[\'data_health\'].get(\'age_seconds\')}s")'
)

needle = '''        signal_table.append({
            "Symbol": r["Symbol"], "Validated Strategy": vr["strategy"],
            "Current Signal": "ACTIVE" if x["active"] else "INACTIVE",
            "Risk-sized Qty": int(x["qty"]), "Stop %": float(vr.get("stop_pct", 0)),
            "Target %": float(vr.get("target_pct", 0)),
        })
'''
replacement = '''        signal_table.append({
            "Symbol": r["Symbol"], "Validated Strategy": vr["strategy"],
            "Current Signal": "ACTIVE" if x["active"] else "INACTIVE",
            "Risk-sized Qty": int(x["qty"]), "Stop %": float(vr.get("stop_pct", 0)),
            "Target %": float(vr.get("target_pct", 0)),
            "Market Data": x["data_health"].get("label"),
            "Quote Age s": x["data_health"].get("age_seconds"),
            "Realtime": x["realtime"],
        })
'''
if needle not in text:
    raise SystemExit("Could not extend signal table.")
text = text.replace(needle, replacement, 1)

needle = '''        "us_market_health": health if "health" in locals() else None,
    }
'''
replacement = '''        "us_market_health": health if "health" in locals() else None,
        "market_data_policy": {
            "nordic_open_market_max_quote_age_seconds": V18_NORDIC_MAX_QUOTE_AGE_SECONDS,
            "block_stale_open_market_paper_entries": V18_REQUIRE_FRESH_OPEN_MARKET_QUOTES,
            "closed_market_last_quote_allowed_for_display": True,
            "note": "Nordnet public Nordic data may be delayed/non-realtime; V18.4 records source freshness where timestamp metadata is available.",
        },
        "nordic_live_candidate_health": [
            {
                "symbol": str(x["row"]["Symbol"]),
                "realtime": bool(x["realtime"]),
                "quote_timestamp": x["quote_timestamp"],
                "market_data": x["data_health"],
            }
            for x in (live_candidates if "live_candidates" in locals() else [])
        ][:25],
    }
'''
if needle not in text:
    raise SystemExit("Could not extend status payload.")
text = text.replace(needle, replacement, 1)

text = text.replace('st.caption("V18.3.1 NEWS + QUANT ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Signals are research outputs, not guaranteed forecasts.")',
                    'st.caption("V18.4 MARKET-DATA INTEGRITY + NEWS + QUANT ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Signals are research outputs, not guaranteed forecasts.")')

p.write_text(text, encoding="utf-8")
try:
    py_compile.compile(str(p), doraise=True)
except Exception:
    shutil.copy2(backup, p)
    raise

print(f"PATCHED: {p}")
print(f"BACKUP:  {backup}")
print("SYNTAX:  PASS")
print("ENGINE:  V18.4-MARKETDATA-1.4")
print("DATA:    Nordic quote timestamp/age + stale-open-market gating + heartbeat diagnostics")
print("PAPER:   no brokerage order endpoints added")
