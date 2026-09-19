from pathlib import Path
import shutil, sys, py_compile

p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dashboard.py")
text = p.read_text(encoding="utf-8")
required = 'V18.4-MARKETDATA-1.4'
if required not in text:
    raise SystemExit("Required V18.4 engine marker not found; refusing to patch.")
backup = p.with_name("dashboard_V18_4_BEFORE_V18_4_1.py")
shutil.copy2(p, backup)

text = text.replace('V16_ENGINE_VERSION = "V18.4-MARKETDATA-1.4"', 'V16_ENGINE_VERSION = "V18.4.1-PAPER-SAFETY-1.41"', 1)

old = 'executable = bool((not V18_REQUIRE_FRESH_OPEN_MARKET_QUOTES) or (not session["open"]) or (not stale))'
new = 'executable = bool(session["open"] and ((not V18_REQUIRE_FRESH_OPEN_MARKET_QUOTES) or (not stale)))'
if old not in text:
    raise SystemExit("Could not locate Nordic executable rule.")
text = text.replace(old, new, 1)

old = 'if x["active"] and x["qty"] > 0 and (not x["data_health"].get("market_open") or not x["data_health"].get("stale")) and int(position_book.get(int(x["row"]["Instrument ID"]), {}).get("qty", 0)) == 0'
new = 'if x["active"] and x["qty"] > 0 and x["data_health"].get("executable", False) and int(position_book.get(int(x["row"]["Instrument ID"]), {}).get("qty", 0)) == 0'
if old not in text:
    raise SystemExit("Could not locate eligible entry market-data rule.")
text = text.replace(old, new, 1)

old = '''    if sell_px is None or fx is None:
        continue
    if health_q.get("market_open") and health_q.get("stale"):
        continue
'''
new = '''    if sell_px is None or fx is None:
        continue
    # Closed, stale, or timestamp-unknown Nordic quotes may be displayed/marked,
    # but must never create a simulated fill.
    if not health_q.get("executable", False):
        continue
'''
if old not in text:
    raise SystemExit("Could not locate exit market-data gate.")
text = text.replace(old, new, 1)

old = '''        "market_data_policy": {
            "nordic_open_market_max_quote_age_seconds": V18_NORDIC_MAX_QUOTE_AGE_SECONDS,
            "block_stale_open_market_paper_entries": V18_REQUIRE_FRESH_OPEN_MARKET_QUOTES,
            "closed_market_last_quote_allowed_for_display": True,
            "note": "Nordnet public Nordic data may be delayed/non-realtime; V18.4 records source freshness where timestamp metadata is available.",
        },
'''
new = '''        "market_data_policy": {
            "nordic_open_market_max_quote_age_seconds": V18_NORDIC_MAX_QUOTE_AGE_SECONDS,
            "block_stale_open_market_paper_entries": V18_REQUIRE_FRESH_OPEN_MARKET_QUOTES,
            "require_market_open_for_paper_fills": True,
            "require_actionable_quote_for_entries_and_exits": True,
            "closed_market_last_quote_allowed_for_display": True,
            "closed_market_last_quote_actionable": False,
            "note": "Nordnet public Nordic data may be delayed/non-realtime; quotes are display/research only unless the Nordic market is open and quote freshness passes.",
        },
'''
if old not in text:
    raise SystemExit("Could not locate market_data_policy.")
text = text.replace(old, new, 1)

# Improve decision explanation if a validated active signal exists but market data blocks it.
old = '''proposed = "WAIT"
proposed_reason = "No validated candidate with an active current entry signal."
action = None
'''
new = '''proposed = "WAIT"
proposed_reason = "No validated candidate with an active current entry signal."
action = None
blocked_market_candidates = [
    x for x in live_candidates
    if x["active"] and x["qty"] > 0
    and int(position_book.get(int(x["row"]["Instrument ID"]), {}).get("qty", 0)) == 0
    and not x["data_health"].get("executable", False)
]
if blocked_market_candidates:
    bx = blocked_market_candidates[0]
    proposed_reason = (
        f"Validated active candidate {bx['row']['Symbol']} blocked from PAPER execution: "
        f"{bx['data_health'].get('label', 'market data not actionable')}."
    )
'''
if old not in text:
    raise SystemExit("Could not locate proposed decision block.")
text = text.replace(old, new, 1)

# Add explicit execution gate telemetry.
old = '''        "active_entry_count": len(eligible_entries) if "eligible_entries" in locals() else 0,
'''
new = '''        "active_entry_count": len(eligible_entries) if "eligible_entries" in locals() else 0,
        "market_data_blocked_active_candidates": len(blocked_market_candidates) if "blocked_market_candidates" in locals() else 0,
'''
if old not in text:
    raise SystemExit("Could not locate active_entry_count.")
text = text.replace(old, new, 1)

text = text.replace(
    'st.caption("V18.4 MARKET-DATA INTEGRITY + NEWS + QUANT ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Signals are research outputs, not guaranteed forecasts.")',
    'st.caption("V18.4.1 PAPER-SAFETY + MARKET-DATA INTEGRITY + NEWS + QUANT ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Closed/stale quotes cannot create paper fills.")',
    1
)

p.write_text(text, encoding="utf-8")
try:
    py_compile.compile(str(p), doraise=True)
except Exception:
    shutil.copy2(backup, p)
    raise

print(f"PATCHED: {p}")
print(f"BACKUP:  {backup}")
print("SYNTAX:  PASS")
print("ENGINE:  V18.4.1-PAPER-SAFETY-1.41")
print("SAFETY:  paper BUY/SELL fills require Nordic market OPEN + actionable quote")
print("CLOSED:  last quote remains display/research only")
print("PAPER:   no brokerage order endpoints added")
