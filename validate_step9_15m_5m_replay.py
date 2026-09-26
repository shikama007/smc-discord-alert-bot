"""
STEP 9 — 15M / 5M HISTORICAL ENTRY REPLAY

Purpose:
- Reuse the production engine definitions without running its live runner.
- Start ONLY after the six proven V3.8 Step-8 production confirmations.
- Evaluate 15M setup and 5M entry as diagnostics, not production signals.
- Compare progressively stricter rule combinations to decide which rules
  should become mandatory.

No production engine code is modified by this validator.
"""
from pathlib import Path
import ast
import time
import requests
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "LTC_USDT"

# Proven V3.8 production Step-8 anchors.
CASES = [
    {"case_id": 1, "step8_time": "2026-09-08 18:00:00", "direction": "BEARISH"},
    {"case_id": 2, "step8_time": "2026-09-20 08:00:00", "direction": "BEARISH"},
    {"case_id": 3, "step8_time": None, "direction": "BEARISH"},
    {"case_id": 4, "step8_time": None, "direction": "BULLISH"},
    {"case_id": 5, "step8_time": "2026-09-26 00:00:00", "direction": "BULLISH"},
    {"case_id": 6, "step8_time": "2026-09-26 09:00:00", "direction": "BULLISH"},
]

# We do NOT fabricate an entry start for cases without a production Step-8
# confirmation. Those cases remain excluded from the strict entry lifecycle.


def load_engine(path):
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    kept = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            kept.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            kept.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kept.append(node)
        # Skip executable module-level runner blocks.
    module = ast.Module(body=kept, type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"__name__": "step9_replay", "__file__": str(path)}
    exec(compile(module, str(path), "exec"), ns)
    return ns


engine = load_engine(ENGINE_FILE)


def fetch_candles(interval, start, end):
    params = {
        "interval": interval,
        "start": int(pd.Timestamp(start).timestamp()),
        "end": int(pd.Timestamp(end).timestamp()),
    }
    r = requests.get(BASE_URL + "/" + SYMBOL, params=params, timeout=20)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(f"MEXC error: {payload}")
    d = payload["data"]
    df = pd.DataFrame({
        "time": d["time"],
        "open": d["open"],
        "high": d["high"],
        "low": d["low"],
        "close": d["close"],
        "volume": d["vol"],
    })
    df["time"] = pd.to_datetime(df["time"], unit="s")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().sort_values("time").drop_duplicates("time").reset_index(drop=True)


def closed_asof(df, asof):
    # Only candles whose close time is <= asof are used. MEXC candle timestamp
    # represents the candle start, so exclude the candle currently in progress
    # by requiring one full interval before the as-of timestamp.
    asof = pd.Timestamp(asof)
    out = df[df["time"] <= asof].copy()
    if len(out) > 1:
        out = out.iloc[:-1].copy()
    return out.reset_index(drop=True)


def safe_poi_context(df, direction):
    if len(df) < 30:
        return [], []
    highs, lows = engine["find_swings"](df, engine.get("SWING_STRENGTH", 2))
    bias = engine["determine_initial_bias"](highs, lows)
    events = engine["detect_historical_events"](df, highs, lows)
    pdx = engine["analyze_premium_discount"](df, highs, lows, bias)
    fvgs = engine["detect_fvgs"](df)
    obs = engine["detect_order_blocks"](df, events)
    sd = engine["detect_supply_demand"](df, events)
    preferred, all_pois = engine["select_relevant_pois"](
        fvgs, obs, sd, float(df.iloc[-1]["close"]), bias,
        premium_discount=pdx, events=events, df=df
    )
    return preferred, all_pois


def direction_events(df, direction, after=None):
    if len(df) < 30:
        return []
    highs, lows = engine["find_swings"](df, engine.get("SWING_STRENGTH", 2))
    events = engine["detect_historical_events"](df, highs, lows)
    out = [e for e in events if e.get("direction") == direction]
    if after is not None:
        out = [e for e in out if pd.Timestamp(e["time"]) > pd.Timestamp(after)]
    return out


def sweep_flags(df, direction):
    if len(df) < 35:
        return [], False, None
    highs, lows = engine["find_swings"](df, engine.get("SWING_STRENGTH", 2))
    try:
        preferred, all_pois = safe_poi_context(df, direction)
        sweeps = engine["detect_liquidity_sweeps"](
            df, highs, lows, preferred_pois=preferred,
            all_pois=all_pois, lookback=engine.get("SWEEP_LOOKBACK", 30)
        )
    except Exception:
        sweeps = []
    aligned = [s for s in sweeps if s.get("direction") == direction]
    if not aligned:
        return [], False, None
    latest = max(aligned, key=lambda x: pd.Timestamp(x["time"]))
    return aligned, True, latest


def poi_touched_before(df, direction, event_time, lookback=12):
    if len(df) < 35:
        return False
    snap = df[df["time"] <= pd.Timestamp(event_time)].copy()
    if len(snap) < 35:
        return False
    preferred, all_pois = safe_poi_context(snap, direction)
    # Use every production-detected POI that existed before the event.
    candidates = [p for p in all_pois if p.get("direction") == direction]
    recent = snap.tail(lookback)
    for _, c in recent.iterrows():
        for p in candidates:
            pt = p.get("time")
            if pt is not None and pd.Timestamp(pt) >= c["time"]:
                continue
            if float(c["high"]) >= float(p["lower"]) and float(c["low"]) <= float(p["upper"]):
                return True
    return False


def displacement_before(df, direction, event_time):
    snap = df[df["time"] <= pd.Timestamp(event_time)].copy()
    if len(snap) < 40:
        return False, None
    sweeps, has_sweep, latest = sweep_flags(snap, direction)
    if not has_sweep:
        return False, None
    # Test each aligned sweep from newest backwards, but require the
    # production displacement to be confirmed before/equal to the event.
    for sw in sorted(sweeps, key=lambda x: pd.Timestamp(x["time"]), reverse=True):
        try:
            d = engine["detect_displacement"](snap, sw, direction)
            if d.get("confirmed") and pd.Timestamp(d["time"]) <= pd.Timestamp(event_time):
                return True, d
        except Exception:
            pass
    return False, None


def retest_after_break(df, event, direction, max_bars=12):
    level = event.get("price")
    if level is None:
        return False
    t = pd.Timestamp(event["time"])
    future = df[df["time"] > t].head(max_bars)
    for _, c in future.iterrows():
        hi, lo, close = map(float, [c["high"], c["low"], c["close"]])
        if direction == "BULLISH" and lo <= float(level) and close > float(level):
            return True
        if direction == "BEARISH" and hi >= float(level) and close < float(level):
            return True
    return False


def analyze_15m(df, case):
    s8 = pd.Timestamp(case["step8_time"])
    direction = case["direction"]
    # 15M setup window: 24h after Step-8.
    future = df[df["time"] > s8].copy()
    for _, row in future.iterrows():
        asof = row["time"]
        snap = df[df["time"] <= asof].copy()
        if len(snap) < 40:
            continue
        evs = direction_events(snap, direction, after=s8)
        if not evs:
            continue
        ev = evs[0]
        # Do not use an event that is beyond the current candle.
        if pd.Timestamp(ev["time"]) > asof:
            continue
        poi = poi_touched_before(snap, direction, ev["time"])
        sweeps, has_sweep, sw = sweep_flags(snap, direction)
        sw_before = any(pd.Timestamp(x["time"]) > s8 and pd.Timestamp(x["time"]) <= pd.Timestamp(ev["time"]) for x in sweeps)
        disp, disp_obj = displacement_before(snap, direction, ev["time"])
        return {
            "setup_time": ev["time"],
            "setup_event": ev.get("event"),
            "setup_level": ev.get("price"),
            "poi": bool(poi),
            "sweep": bool(sw_before),
            "displacement": bool(disp),
            "retest": bool(retest_after_break(df, ev, direction)),
            "setup_candle": asof,
        }
    return None


def analyze_5m(df, setup, direction):
    if not setup:
        return None
    start = pd.Timestamp(setup["setup_time"])
    future = df[df["time"] > start].copy()
    for _, row in future.iterrows():
        asof = row["time"]
        snap = df[df["time"] <= asof].copy()
        if len(snap) < 50:
            continue
        evs = direction_events(snap, direction, after=start)
        if not evs:
            continue
        ev = evs[0]
        if pd.Timestamp(ev["time"]) > asof:
            continue
        sweeps, _, _ = sweep_flags(snap, direction)
        sw_before = any(pd.Timestamp(x["time"]) > start and pd.Timestamp(x["time"]) <= pd.Timestamp(ev["time"]) for x in sweeps)
        disp, disp_obj = displacement_before(snap, direction, ev["time"])
        ret = retest_after_break(snap, ev, direction)
        return {
            "entry_structure_time": ev["time"],
            "entry_event": ev.get("event"),
            "entry_level": ev.get("price"),
            "sweep": bool(sw_before),
            "displacement": bool(disp),
            "retest": bool(ret),
            "entry_candle": asof,
        }
    return None


def main():
    all_rows = []
    case_rows = []
    valid_cases = [c for c in CASES if c["step8_time"]]

    for case in valid_cases:
        s8 = pd.Timestamp(case["step8_time"])
        # Enough context for swing/POI detection, plus the forward setup window.
        m15 = fetch_candles("Min15", s8 - pd.Timedelta(hours=24), s8 + pd.Timedelta(hours=36))
        setup = analyze_15m(m15, case)
        m5 = None
        entry = None
        if setup:
            setup_t = pd.Timestamp(setup["setup_time"])
            m5 = fetch_candles("Min5", setup_t - pd.Timedelta(hours=6), setup_t + pd.Timedelta(hours=18))
            entry = analyze_5m(m5, setup, case["direction"])

        row = {
            "case_id": case["case_id"],
            "step8_time": s8,
            "direction": case["direction"],
            "15m_setup_found": bool(setup),
            "15m_setup_time": setup.get("setup_time") if setup else None,
            "15m_setup_event": setup.get("setup_event") if setup else None,
            "15m_poi": setup.get("poi") if setup else False,
            "15m_sweep": setup.get("sweep") if setup else False,
            "15m_displacement": setup.get("displacement") if setup else False,
            "15m_retest": setup.get("retest") if setup else False,
            "5m_entry_structure_found": bool(entry),
            "5m_entry_time": entry.get("entry_structure_time") if entry else None,
            "5m_entry_event": entry.get("entry_event") if entry else None,
            "5m_sweep": entry.get("sweep") if entry else False,
            "5m_displacement": entry.get("displacement") if entry else False,
            "5m_retest": entry.get("retest") if entry else False,
        }
        case_rows.append(row)

        p15_rules = [
            ("structure", bool(setup)),
            ("structure_plus_poi", bool(setup and setup["poi"])),
            ("structure_plus_sweep", bool(setup and setup["sweep"])),
            ("structure_plus_poi_plus_sweep", bool(setup and setup["poi"] and setup["sweep"])),
            ("structure_plus_poi_sweep_displacement", bool(setup and setup["poi"] and setup["sweep"] and setup["displacement"])),
        ]
        p5_rules = [
            ("structure", bool(entry)),
            ("sweep_plus_structure", bool(entry and entry["sweep"])),
            ("sweep_displacement_plus_structure", bool(entry and entry["sweep"] and entry["displacement"])),
            ("sweep_displacement_structure_retest", bool(entry and entry["sweep"] and entry["displacement"] and entry["retest"])),
        ]
        for p15_name, p15_ok in p15_rules:
            for p5_name, p5_ok in p5_rules:
                all_rows.append({
                    "case_id": case["case_id"],
                    "direction": case["direction"],
                    "15m_rule": p15_name,
                    "5m_rule": p5_name,
                    "qualified": bool(p15_ok and p5_ok),
                })

    cases_df = pd.DataFrame(case_rows)
    matrix_df = pd.DataFrame(all_rows)
    if not matrix_df.empty:
        summary = (
            matrix_df.groupby(["15m_rule", "5m_rule"], as_index=False)["qualified"]
            .agg(["sum", "count"])
            .reset_index()
            .rename(columns={"sum": "qualified_cases", "count": "eligible_cases"})
        )
        summary["qualification_rate_pct"] = summary["qualified_cases"] / summary["eligible_cases"] * 100
    else:
        summary = pd.DataFrame()

    cases_df.to_csv("step9_15m_5m_cases.csv", index=False)
    matrix_df.to_csv("step9_15m_5m_matrix.csv", index=False)
    summary.to_csv("step9_15m_5m_summary.csv", index=False)

    print("\n=== STEP 9 15M/5M HISTORICAL REPLAY ===")
    print(f"Step-8-confirmed anchor cases: {len(valid_cases)}")
    print(f"15M setup found: {int(cases_df['15m_setup_found'].sum()) if not cases_df.empty else 0}")
    print(f"5M entry structure found: {int(cases_df['5m_entry_structure_found'].sum()) if not cases_df.empty else 0}")
    print("\nCase details:")
    if not cases_df.empty:
        print(cases_df.to_string(index=False))
    print("\nReports:")
    print(" - step9_15m_5m_cases.csv")
    print(" - step9_15m_5m_matrix.csv")
    print(" - step9_15m_5m_summary.csv")


if __name__ == "__main__":
    main()
