"""
STEP 9.2 — ENTRY TRIGGER REPLAY

Purpose:
- Start ONLY from the 4 proven production Step-8 confirmations.
- Replay three candidate 15M/5M entry trigger models.
- Do NOT modify the production engine.
- Use closed candles only.
- Record first qualifying entry, entry price, SL, TP(2R), RR, and outcome.

Candidate models:
A_LOOSE:
    15M Structure + 15M POI
    -> 5M Structure + 5M Retest
    -> Entry

B_MEDIUM:
    15M Structure + 15M POI
    -> (15M Sweep OR 15M Retest)
    -> 5M Structure + 5M Retest
    -> Entry

C_SWEEP_CONFIRMED:
    15M Structure + 15M POI
    -> 15M Sweep
    -> 5M Structure + 5M Sweep + 5M Retest
    -> Entry

Test-only trade model:
- Entry = close of the first qualifying 5M retest candle.
- SL = beyond that retest candle wick.
- TP = 2R (standardized comparison target).
- No live orders are created.
- This is diagnostic evidence, not a production trading rule.
"""

from pathlib import Path
import ast
import requests
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "LTC_USDT"

CASES = [
    {"case_id": 1, "step8_time": "2026-09-08 18:00:00", "direction": "BEARISH"},
    {"case_id": 2, "step8_time": "2026-09-20 08:00:00", "direction": "BEARISH"},
    {"case_id": 5, "step8_time": "2026-09-26 00:00:00", "direction": "BULLISH"},
    {"case_id": 6, "step8_time": "2026-09-26 09:00:00", "direction": "BULLISH"},
]

MODELS = ["A_LOOSE", "B_MEDIUM", "C_SWEEP_CONFIRMED"]


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
    module = ast.Module(body=kept, type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"__name__": "step9_2_replay", "__file__": str(path)}
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
    return (
        df.dropna()
        .sort_values("time")
        .drop_duplicates("time")
        .reset_index(drop=True)
    )


def structure_events(df, direction, after=None):
    if len(df) < 40:
        return []
    highs, lows = engine["find_swings"](
        df, engine.get("SWING_STRENGTH", 2)
    )
    events = engine["detect_historical_events"](df, highs, lows)
    out = []
    for e in events:
        if e.get("direction") != direction:
            continue
        t = e.get("time")
        if t is None:
            continue
        if after is not None and pd.Timestamp(t) <= pd.Timestamp(after):
            continue
        out.append(e)
    return sorted(out, key=lambda x: pd.Timestamp(x["time"]))


def poi_context(df):
    if len(df) < 40:
        return [], []
    highs, lows = engine["find_swings"](
        df, engine.get("SWING_STRENGTH", 2)
    )
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


def poi_before_event(df, direction, event_time, lookback_bars=20):
    snap = df[df["time"] <= pd.Timestamp(event_time)].copy()
    if len(snap) < 40:
        return False, None

    try:
        preferred, all_pois = poi_context(snap)
    except Exception:
        return False, None

    candidates = [
        p for p in all_pois
        if p.get("direction") == direction
        and p.get("time") is not None
        and pd.Timestamp(p["time"]) < pd.Timestamp(event_time)
    ]

    recent = snap.tail(lookback_bars)
    for _, c in recent.iterrows():
        for p in candidates:
            if (
                float(c["high"]) >= float(p["lower"])
                and float(c["low"]) <= float(p["upper"])
            ):
                return True, p
    return False, None


def sweeps_between(df, direction, start_time, end_time):
    snap = df[df["time"] <= pd.Timestamp(end_time)].copy()
    if len(snap) < 40:
        return []

    try:
        highs, lows = engine["find_swings"](
            snap, engine.get("SWING_STRENGTH", 2)
        )
        preferred, all_pois = poi_context(snap)
        sweeps = engine["detect_liquidity_sweeps"](
            snap, highs, lows,
            preferred_pois=preferred,
            all_pois=all_pois,
            lookback=engine.get("SWEEP_LOOKBACK", 30),
        )
    except Exception:
        return []

    return [
        s for s in sweeps
        if s.get("direction") == direction
        and s.get("time") is not None
        and pd.Timestamp(start_time) < pd.Timestamp(s["time"]) <= pd.Timestamp(end_time)
    ]


def retest_candle(df, event, direction, max_bars=12):
    if not event or event.get("price") is None or event.get("time") is None:
        return None

    level = float(event["price"])
    t = pd.Timestamp(event["time"])
    future = df[df["time"] > t].head(max_bars)

    for _, c in future.iterrows():
        hi = float(c["high"])
        lo = float(c["low"])
        close = float(c["close"])

        if direction == "BULLISH" and lo <= level and close > level:
            return c
        if direction == "BEARISH" and hi >= level and close < level:
            return c

    return None


def first_structure(df, direction, after):
    events = structure_events(df, direction, after=after)
    return events[0] if events else None


def first_entry_for_case(m15, m5, case):
    direction = case["direction"]
    step8 = pd.Timestamp(case["step8_time"])

    # 15M setup
    s15 = first_structure(m15, direction, step8)
    if not s15:
        return None

    poi_ok, poi = poi_before_event(m15, direction, s15["time"])
    if not poi_ok:
        return None

    sweeps15 = sweeps_between(m15, direction, step8, s15["time"])

    # 5M refinement starts after the 15M structure event.
    s5 = first_structure(m5, direction, s15["time"])
    if not s5:
        return None

    sweeps5 = sweeps_between(m5, direction, s15["time"], s5["time"])
    r5 = retest_candle(m5, s5, direction)

    return {
        "s15": s15,
        "poi": poi,
        "sweeps15": sweeps15,
        "s5": s5,
        "sweeps5": sweeps5,
        "retest5": r5,
    }


def build_trade(signal, direction):
    if signal is None or signal["retest5"] is None:
        return None

    c = signal["retest5"]
    entry = float(c["close"])
    wick = float(c["low"] if direction == "BULLISH" else c["high"])

    if direction == "BULLISH":
        sl = wick
        risk = entry - sl
        if risk <= 0:
            return None
        tp = entry + 2.0 * risk
    else:
        sl = wick
        risk = sl - entry
        if risk <= 0:
            return None
        tp = entry - 2.0 * risk

    return {
        "entry_time": c["time"],
        "entry_price": entry,
        "sl_price": sl,
        "tp_2r_price": tp,
        "risk_distance": risk,
        "rr_target": 2.0,
    }


def outcome_after(df, trade, direction, max_bars=72):
    if not trade:
        return {"outcome": "NO_TRADE", "exit_time": None}

    t = pd.Timestamp(trade["entry_time"])
    future = df[df["time"] > t].head(max_bars)

    for _, c in future.iterrows():
        hi = float(c["high"])
        lo = float(c["low"])

        if direction == "BULLISH":
            # Conservative: if both hit in one candle, count SL first.
            if lo <= trade["sl_price"] and hi >= trade["tp_2r_price"]:
                return {"outcome": "BOTH_SAME_CANDLE_SL_FIRST", "exit_time": c["time"]}
            if lo <= trade["sl_price"]:
                return {"outcome": "SL", "exit_time": c["time"]}
            if hi >= trade["tp_2r_price"]:
                return {"outcome": "TP_2R", "exit_time": c["time"]}
        else:
            if hi >= trade["sl_price"] and lo <= trade["tp_2r_price"]:
                return {"outcome": "BOTH_SAME_CANDLE_SL_FIRST", "exit_time": c["time"]}
            if hi >= trade["sl_price"]:
                return {"outcome": "SL", "exit_time": c["time"]}
            if lo <= trade["tp_2r_price"]:
                return {"outcome": "TP_2R", "exit_time": c["time"]}

    return {"outcome": "OPEN_AFTER_WINDOW", "exit_time": None}


def model_qualifies(model, sig):
    if not sig:
        return False

    has15s = bool(sig["s15"])
    haspoi = bool(sig["poi"])
    has15sw = bool(sig["sweeps15"])
    has5s = bool(sig["s5"])
    has5sw = bool(sig["sweeps5"])
    has5r = sig["retest5"] is not None

    if model == "A_LOOSE":
        return has15s and haspoi and has5s and has5r

    if model == "B_MEDIUM":
        return has15s and haspoi and (has15sw or has5r) and has5s and has5r

    if model == "C_SWEEP_CONFIRMED":
        return has15s and haspoi and has15sw and has5s and has5sw and has5r

    return False


def main():
    rows = []

    for case in CASES:
        s8 = pd.Timestamp(case["step8_time"])

        # Enough context before Step 8 + enough time after for entry/outcome.
        m15 = fetch_candles(
            "Min15",
            s8 - pd.Timedelta(hours=36),
            s8 + pd.Timedelta(hours=60),
        )

        # Use a broad 5M window; exact setup anchor is found from 15M.
        m5 = fetch_candles(
            "Min5",
            s8 - pd.Timedelta(hours=12),
            s8 + pd.Timedelta(hours=60),
        )

        sig = first_entry_for_case(m15, m5, case)

        for model in MODELS:
            qualified = model_qualifies(model, sig)
            trade = build_trade(sig, case["direction"]) if qualified else None
            result = outcome_after(m5, trade, case["direction"]) if trade else {
                "outcome": "NO_TRADE", "exit_time": None
            }

            rows.append({
                "case_id": case["case_id"],
                "step8_time": case["step8_time"],
                "direction": case["direction"],
                "model": model,

                "15m_structure_time": sig["s15"]["time"] if sig else None,
                "15m_structure_event": sig["s15"].get("event") if sig else None,
                "15m_structure_level": sig["s15"].get("price") if sig else None,
                "15m_poi_time": sig["poi"].get("time") if sig else None,
                "15m_sweep_time": (
                    sig["sweeps15"][-1].get("time")
                    if sig and sig["sweeps15"] else None
                ),

                "5m_structure_time": sig["s5"]["time"] if sig else None,
                "5m_structure_event": sig["s5"].get("event") if sig else None,
                "5m_structure_level": sig["s5"].get("price") if sig else None,
                "5m_sweep_time": (
                    sig["sweeps5"][-1].get("time")
                    if sig and sig["sweeps5"] else None
                ),
                "5m_retest_time": (
                    sig["retest5"]["time"]
                    if sig and sig["retest5"] is not None else None
                ),

                "qualified": qualified,
                "entry_time": trade["entry_time"] if trade else None,
                "entry_price": trade["entry_price"] if trade else None,
                "sl_price": trade["sl_price"] if trade else None,
                "tp_2r_price": trade["tp_2r_price"] if trade else None,
                "risk_distance": trade["risk_distance"] if trade else None,
                "rr_target": trade["rr_target"] if trade else None,
                "outcome": result["outcome"],
                "exit_time": result["exit_time"],
            })

    df = pd.DataFrame(rows)
    summary = (
        df.groupby("model", as_index=False)
        .agg(
            cases=("case_id", "count"),
            qualified_cases=("qualified", "sum"),
            tp_2r=("outcome", lambda s: int((s == "TP_2R").sum())),
            sl=("outcome", lambda s: int((s == "SL").sum())),
            open_after_window=("outcome", lambda s: int((s == "OPEN_AFTER_WINDOW").sum())),
            no_trade=("outcome", lambda s: int((s == "NO_TRADE").sum())),
        )
    )
    summary["qualification_rate_pct"] = (
        summary["qualified_cases"] / summary["cases"] * 100
    )
    qualified = summary["qualified_cases"].replace(0, pd.NA)
    summary["tp_rate_among_qualified_pct"] = (
        summary["tp_2r"] / qualified * 100
    )

    df.to_csv("step9_2_entry_trigger_cases.csv", index=False)
    summary.to_csv("step9_2_entry_trigger_summary.csv", index=False)

    print("\n=== STEP 9.2 ENTRY TRIGGER REPLAY ===")
    print(f"Anchor cases: {len(CASES)}")
    print("\nSummary:")
    print(summary.to_string(index=False))
    print("\nCase/model details:")
    print(df.to_string(index=False))
    print("\nReports:")
    print(" - step9_2_entry_trigger_cases.csv")
    print(" - step9_2_entry_trigger_summary.csv")


if __name__ == "__main__":
    main()
