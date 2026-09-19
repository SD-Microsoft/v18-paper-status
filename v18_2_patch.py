from pathlib import Path
import sys, shutil, py_compile, re

if len(sys.argv) != 2:
    raise SystemExit("Usage: py v18_2_patch.py <dashboard.py>")
p=Path(sys.argv[1]).resolve()
if not p.exists(): raise SystemExit(f"Not found: {p}")
src=p.read_text(encoding="utf-8")
bak=p.with_name("dashboard_V18_1_BEFORE_V18_2.py")
shutil.copy2(p,bak)

# Require V18.1 base.
if 'V18.1-BEARPROOF-1.1' not in src:
    raise SystemExit("ABORT: V18.1 base not detected.")

src=src.replace('V16_ENGINE_VERSION = "V18.1-BEARPROOF-1.1"','V16_ENGINE_VERSION = "V18.2-BEARPROOF-1.2"',1)
src=src.replace('st.write("### V18 Failure Envelope / Capital Reality Check")','st.write("### V18.2 Failure Envelope / Capital Reality Check")',1)
src=src.replace('st.caption("V18 treats stop-loss, take-profit, costs, slippage, held-out data and walk-forward results as part of the strategy—not optional extras.")',
                'st.caption("V18.2 treats stop-loss, take-profit, costs, slippage, held-out data and walk-forward results as part of the strategy—not optional extras.")',1)
src=src.replace('"Simulation only. When enabled, eligible V14 BUY/EXIT actions are written to the local paper-trade CSV. No Nordnet order endpoint is called."',
                '"Simulation only. When enabled, eligible V18.2 BUY/EXIT actions are written to the local paper-trade CSV. No Nordnet order endpoint is called."',1)
src=src.replace('st.caption("V18 BEARPROOF ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Signals are research outputs, not guaranteed forecasts.")',
                'st.caption("V18.2 BEARPROOF ENGINE • 2,000 NOK PAPER LAB • NO BROKERAGE ORDER ENDPOINTS • Signals are research outputs, not guaranteed forecasts.")',1)

# Make allocation an explicit backtest input and pass configured allocation through validation.
src=src.replace(
'''def run_backtest(price_df, fee_rate=COMMISSION_RATE, min_fee=MIN_COMMISSION,
                 fx_rate=1.0, initial_cash=2000.0, strategy_name="EMA + MACD",
                 slippage_bps=5.0, stop_pct=7.0, take_profit_pct=14.0):''',
'''def run_backtest(price_df, fee_rate=COMMISSION_RATE, min_fee=MIN_COMMISSION,
                 fx_rate=1.0, initial_cash=2000.0, strategy_name="EMA + MACD",
                 slippage_bps=5.0, stop_pct=7.0, take_profit_pct=14.0,
                 max_alloc_pct=20.0):''',1)
src=src.replace('    alloc_fraction = 0.20\n    slip_side', '    alloc_fraction = max(0.0, min(float(max_alloc_pct), 100.0)) / 100.0\n    slip_side',1)

# Add trade quality metrics.
needle='''    entries = int((df["POSITION"].diff() == 1).sum() + (1 if df["POSITION"].iloc[0] == 1 else 0))
    stats = {
'''
replacement='''    entries = int((df["POSITION"].diff() == 1).sum() + (1 if df["POSITION"].iloc[0] == 1 else 0))
    completed = []
    entry_equity = None
    for j in range(len(df)):
        pos = int(df["POSITION"].iloc[j])
        prev = int(df["POSITION"].iloc[j-1]) if j else 0
        if pos == 1 and prev == 0:
            entry_equity = float(df["EQUITY"].iloc[j])
        elif pos == 0 and prev == 1 and entry_equity and entry_equity > 0:
            completed.append(float(df["EQUITY"].iloc[j]) / entry_equity - 1.0)
            entry_equity = None
    wins = [x for x in completed if x > 0]
    losses = [x for x in completed if x < 0]
    win_rate = (len(wins) / len(completed) * 100.0) if completed else None
    avg_trade = (sum(completed) / len(completed) * 100.0) if completed else None
    stats = {
'''
if needle not in src: raise SystemExit("ABORT: stats anchor not found")
src=src.replace(needle,replacement,1)
src=src.replace('''        "Target exits": int((df["EXIT_REASON"] == "TARGET").sum()),
    }''',
'''        "Target exits": int((df["EXIT_REASON"] == "TARGET").sum()),
        "Completed trades": len(completed),
        "Win rate %": win_rate,
        "Avg completed trade %": avg_trade,
    }''',1)

# Validation signature and all candidate search/backtests use current max allocation.
src=src.replace('def candidate_parameter_search(price_df, capital_nok, fx, slippage_bps=5.0):',
                'def candidate_parameter_search(price_df, capital_nok, fx, slippage_bps=5.0, max_alloc_pct=20.0):',1)
src=src.replace('''                    stop_pct=stop, take_profit_pct=target,
                )''',
'''                    stop_pct=stop, take_profit_pct=target, max_alloc_pct=max_alloc_pct,
                )''',1)
src=src.replace('def robust_walk_forward(price_df, capital_nok, fx, slippage_bps=5.0, folds=3):',
                'def robust_walk_forward(price_df, capital_nok, fx, slippage_bps=5.0, folds=3, max_alloc_pct=20.0):',1)
src=src.replace('search = candidate_parameter_search(train, capital_nok, fx, slippage_bps)',
                'search = candidate_parameter_search(train, capital_nok, fx, slippage_bps, max_alloc_pct)',1)
src=src.replace('''            stop_pct=float(best["Stop %"]), take_profit_pct=float(best["Target %"]),
        )''',
'''            stop_pct=float(best["Stop %"]), take_profit_pct=float(best["Target %"]),
            max_alloc_pct=max_alloc_pct,
        )''',1)
src=src.replace('def validate_candidate(price_df, currency, capital_nok, slippage_bps=5.0, stop_pct=7.0, target_pct=14.0):',
                'def validate_candidate(price_df, currency, capital_nok, slippage_bps=5.0, stop_pct=7.0, target_pct=14.0, max_alloc_pct=20.0):',1)
# Remaining relevant calls in validation.
src=src.replace('search = candidate_parameter_search(train, capital_nok, fx, slippage_bps)',
                'search = candidate_parameter_search(train, capital_nok, fx, slippage_bps, max_alloc_pct)',1)
src=src.replace('''        slippage_bps=slippage_bps, stop_pct=chosen_stop, take_profit_pct=chosen_target,
    )''',
'''        slippage_bps=slippage_bps, stop_pct=chosen_stop, take_profit_pct=chosen_target,
        max_alloc_pct=max_alloc_pct,
    )''',2)
src=src.replace('rwf = robust_walk_forward(df, capital_nok, fx, slippage_bps, folds=4)',
                'rwf = robust_walk_forward(df, capital_nok, fx, slippage_bps, folds=4, max_alloc_pct=max_alloc_pct)',1)
# Stress function supports allocation.
src=src.replace('def v18_stress_test(price_df, strategy_name, initial_cash, fx_rate, stop_pct, take_profit_pct):',
                'def v18_stress_test(price_df, strategy_name, initial_cash, fx_rate, stop_pct, take_profit_pct, max_alloc_pct=20.0):',1)
src=src.replace('''            slippage_bps=slip, stop_pct=stop_pct, take_profit_pct=take_profit_pct)''',
'''            slippage_bps=slip, stop_pct=stop_pct, take_profit_pct=take_profit_pct,
            max_alloc_pct=max_alloc_pct)''',1)
src=src.replace('stress = v18_stress_test(test, name, capital_nok, fx, chosen_stop, chosen_target)',
                'stress = v18_stress_test(test, name, capital_nok, fx, chosen_stop, chosen_target, max_alloc_pct)',1)
# Pass dashboard allocation into validator.
old='''                                        bt_stop_pct if "bt_stop_pct" in locals() else 7.0,
                                        bt_target_pct if "bt_target_pct" in locals() else 14.0)'''
new='''                                        bt_stop_pct if "bt_stop_pct" in locals() else 7.0,
                                        bt_target_pct if "bt_target_pct" in locals() else 14.0,
                                        bot_max_alloc)'''
if old not in src: raise SystemExit("ABORT: validator call anchor not found")
src=src.replace(old,new,1)

# Require a minimally meaningful number of completed OOS trades when available.
anchor='''    if test_stats["Max drawdown %"] <= -20:
        passed = False; reasons.append("OOS drawdown <= -20%")
'''
add='''    if test_stats["Max drawdown %"] <= -20:
        passed = False; reasons.append("OOS drawdown <= -20%")
    if int(test_stats.get("Completed trades", 0) or 0) < 2:
        passed = False; reasons.append("fewer than 2 completed OOS trades")
'''
if anchor not in src: raise SystemExit("ABORT: OOS gate anchor not found")
src=src.replace(anchor,add,1)

# Rich diagnostics status assembled from actual validation_results.
old_status='''        "validation_diagnostics": [
            {
                "symbol": str(x.get("Symbol", "")),
                "filters": str(x.get("Candidate Filters", "")),
                "validation": str(x.get("Validation", "")),
                "affordable_qty": int(x.get("Affordable Qty", 0) or 0),
                "reason": str(x.get("Why", "")),
            }
            for x in (validation_rows[:25] if "validation_rows" in locals() else [])
        ],
'''
new_status='''        "validation_diagnostics": [
            {
                "symbol": str(x.get("Symbol", "")),
                "filters": str(x.get("Candidate Filters", "")),
                "validation": str(x.get("Validation", "")),
                "affordable_qty": int(x.get("Affordable Qty", 0) or 0),
                "reason": str(x.get("Why", "")),
                **((lambda vr: {
                    "strategy": vr.get("strategy"),
                    "stop_pct": vr.get("stop_pct"),
                    "target_pct": vr.get("target_pct"),
                    "oos_return_pct": (vr.get("wf") or {}).get("test", {}).get("Strategy return %"),
                    "oos_max_drawdown_pct": (vr.get("wf") or {}).get("test", {}).get("Max drawdown %"),
                    "oos_entries": (vr.get("wf") or {}).get("test", {}).get("Entries"),
                    "oos_completed_trades": (vr.get("wf") or {}).get("test", {}).get("Completed trades"),
                    "oos_win_rate_pct": (vr.get("wf") or {}).get("test", {}).get("Win rate %"),
                    "positive_walk_forward_folds": vr.get("positive_folds"),
                    "walk_forward_folds": (vr.get("rwf").to_dict("records") if hasattr(vr.get("rwf"), "to_dict") else []),
                    "stress_scenarios": (vr.get("stress").to_dict("records") if hasattr(vr.get("stress"), "to_dict") else []),
                }))(validation_results.get(int(next((rr["Instrument ID"] for _, rr in ranked_df.iterrows() if str(rr["Symbol"]) == str(x.get("Symbol", ""))), -1)), {}))
            }
            for x in (validation_rows[:25] if "validation_rows" in locals() else [])
        ],
        "rejection_summary": {
            "filter_fail": sum(1 for x in validation_rows if x.get("Candidate Filters") == "FAIL") if "validation_rows" in locals() else 0,
            "validation_fail": sum(1 for x in validation_rows if x.get("Validation") == "FAIL") if "validation_rows" in locals() else 0,
            "spread_gt_3pct": sum("spread > 3%" in str(x.get("Why", "")) for x in validation_rows) if "validation_rows" in locals() else 0,
            "extreme_volatility": sum("extreme volatility" in str(x.get("Why", "")) for x in validation_rows) if "validation_rows" in locals() else 0,
            "low_score": sum("score +" in str(x.get("Why", "")) for x in validation_rows) if "validation_rows" in locals() else 0,
        },
        "capital_diagnostics": {
            "max_position_budget_nok": round(float(STARTING_BALANCE * bot_max_alloc / 100.0), 2) if "bot_max_alloc" in locals() else None,
            "risk_budget_nok": round(float(STARTING_BALANCE * bot_risk_pct / 100.0), 2) if "bot_risk_pct" in locals() else None,
            "min_fee_round_trip_nok": round(float(2 * MIN_COMMISSION), 2),
            "min_fee_round_trip_pct_of_max_position": round(float((2 * MIN_COMMISSION) / max(STARTING_BALANCE * bot_max_alloc / 100.0, 1e-9) * 100.0), 4) if "bot_max_alloc" in locals() else None,
        },
'''
if old_status not in src: raise SystemExit("ABORT: V18.1 diagnostics block not found")
src=src.replace(old_status,new_status,1)

p.write_text(src,encoding="utf-8")
try:
    py_compile.compile(str(p),doraise=True)
except Exception:
    shutil.copy2(bak,p); raise
print(f"PATCHED: {p}")
print(f"BACKUP:  {bak}")
print("SYNTAX:  PASS")
print("ENGINE:  V18.2-BEARPROOF-1.2")
print("CHANGES: allocation-consistent backtests, richer OOS/trade/stress/WF diagnostics, minimum OOS trade gate, visible V18.2 labels")
