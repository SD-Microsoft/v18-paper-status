from pathlib import Path
import sys, py_compile, shutil, datetime

if len(sys.argv) != 2:
    raise SystemExit("Usage: py v18_1_patch.py <dashboard.py>")

p = Path(sys.argv[1]).resolve()
if not p.exists():
    raise SystemExit(f"Not found: {p}")

src = p.read_text(encoding="utf-8")
backup = p.with_name("dashboard_V18_BEFORE_V18_1_AUTO.py")
shutil.copy2(p, backup)

old_cost = '''    changes = df["POSITION"].diff().abs().fillna(df["POSITION"])
    pct_fee = max(float(fee_rate), float(min_fee) / max(float(initial_cash), 1.0))
    slip = max(float(slippage_bps), 0.0) / 2000.0
    df["COST_DRAG"] = changes * (pct_fee + slip)
    df["STRAT_RET_NET"] = df["STRAT_RET"] - df["COST_DRAG"]
'''
new_cost = '''    changes = df["POSITION"].diff().abs().fillna(df["POSITION"])
    # V18.1: model transaction costs against the capital actually deployed.
    # The backtest is long/flat and uses max 20% allocation, matching the paper-bot default.
    # fx_rate converts the native share price to NOK; fractional shares remain disabled.
    alloc_fraction = 0.20
    slip_side = max(float(slippage_bps), 0.0) / 10000.0
    cost_drag = pd.Series(0.0, index=df.index, dtype=float)
    for j in range(len(df)):
        if float(changes.iloc[j]) <= 0:
            continue
        px_native = float(df["Price"].iloc[j])
        px_nok = px_native * max(float(fx_rate), 1e-12)
        alloc_nok = max(float(initial_cash) * alloc_fraction, 0.0)
        qty = int(alloc_nok // px_nok) if px_nok > 0 else 0
        if qty <= 0:
            # An unaffordable instrument cannot be represented as a valid trade.
            cost_drag.iloc[j] = 1.0
            continue
        gross_nok = qty * px_nok
        fee_nok = max(float(min_fee), gross_nok * float(fee_rate))
        fee_fraction_of_account = fee_nok / max(float(initial_cash), 1.0)
        slippage_fraction_of_account = (gross_nok * slip_side) / max(float(initial_cash), 1.0)
        cost_drag.iloc[j] = fee_fraction_of_account + slippage_fraction_of_account
    df["COST_DRAG"] = cost_drag
    df["STRAT_RET_NET"] = df["STRAT_RET"] * alloc_fraction - df["COST_DRAG"]
'''
if old_cost not in src:
    raise SystemExit("ABORT: expected V18 cost block not found; dashboard left unchanged.")
src = src.replace(old_cost, new_cost, 1)

old_stress = '''    stress = v18_stress_test(df, name, capital_nok, fx, chosen_stop, chosen_target)
'''
new_stress = '''    # V18.1: stress the held-out region rather than reusing the full parameter-selection sample.
    stress = v18_stress_test(test, name, capital_nok, fx, chosen_stop, chosen_target)
'''
if old_stress not in src:
    raise SystemExit("ABORT: expected V18 stress call not found; dashboard left unchanged.")
src = src.replace(old_stress, new_stress, 1)

old_status = '''        "active_entry_count": len(eligible_entries) if "eligible_entries" in locals() else 0,
        "auto_decisions_enabled": bool(auto_enabled) if "auto_enabled" in locals() else False,
'''
new_status = '''        "active_entry_count": len(eligible_entries) if "eligible_entries" in locals() else 0,
        "validation_diagnostics": [
            {
                "symbol": str(x.get("Symbol", "")),
                "filters": str(x.get("Candidate Filters", "")),
                "validation": str(x.get("Validation", "")),
                "affordable_qty": int(x.get("Affordable Qty", 0) or 0),
                "reason": str(x.get("Why", "")),
            }
            for x in (validation_rows[:25] if "validation_rows" in locals() else [])
        ],
        "auto_decisions_enabled": bool(auto_enabled) if "auto_enabled" in locals() else False,
'''
if old_status not in src:
    raise SystemExit("ABORT: expected V18 status block not found; dashboard left unchanged.")
src = src.replace(old_status, new_status, 1)

src = src.replace('V16_ENGINE_VERSION = "V18-BEARPROOF-1.0"', 'V16_ENGINE_VERSION = "V18.1-BEARPROOF-1.1"', 1)
p.write_text(src, encoding="utf-8")
try:
    py_compile.compile(str(p), doraise=True)
except Exception:
    shutil.copy2(backup, p)
    raise
print(f"PATCHED: {p}")
print(f"BACKUP:  {backup}")
print("SYNTAX:  PASS")
print("V18.1 changes: position-aware NOK costs, OOS stress test, validation diagnostics feed")
