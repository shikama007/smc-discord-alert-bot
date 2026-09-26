"""
STEP 9.1 — 15M / 5M COMPONENT DIAGNOSTIC REPLAY

Purpose:
- Start only from the four proven production Step-8 confirmations.
- Diagnose each 15M/5M component independently.
- Do NOT decide final mandatory/optional rules.
- Do NOT modify the production engine.

Components recorded separately:
15M: structure, POI, sweep, displacement, retest
5M: structure, sweep, displacement, retest

Important:
- Only closed candles are used.
- A component is only considered if it occurs after the previous lifecycle anchor.
- No future candle is used to confirm an earlier event.
- The reports preserve case-level evidence so the next rule decision is data-driven.
"""

from pathlib import Path
import ast
import requests
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "LTC_USDT"

# Only production Step-8 confirmed cases from V3.8.
CASES = [
    {"case_id": 1, "step8_time": "2026-09-08 18:00:00", "direction": "BEARISH"},
    {"case_id": 2, "step8_time": "2026-09-20 08:00:00", "direction": "BEARISH"},
    {"case_id": 5, "step8_time": "2026-09-26 00:00:00", "direction": "BULLISH"},
    {"case_id": 6, "step8_time": "2026-09-26 09:00:00", "direction": "BULLISH"},
]


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
    ns = {"__name__": "step9_1_diagnostic", "__file__": str(path)}
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


def closed_asof(df, asof):
    """Exclude the candle currently in progress."""
    asof = pd.Timestamp(asof)
    out = df[df["time"] <= asof].copy()
    if len(out) > 1:
        out = out.iloc[:-1].copy()
    return out.reset_index(drop=True)


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
        fvgs,
        obs,
        sd,
        float(df.iloc[-1]["close"]),
        bias,
        premium_discount=pdx,
        events=events,
        df=df,
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
            if float(c["high"]) >= float(p["lower"]) and float(c["low"]) <= float(p["upper"]):
                return True, p
    return False, None


def sweeps_before_event(df, direction, start_time, event_time):
    snap = df[df["time"] <= pd.Timestamp(event_time)].copy()
    if len(snap) < 40:
        return []

    try:
        highs, lows = engine["find_swings"](
            snap, engine.get("SWING_STRENGTH", 2)
        )
        preferred, all_pois = poi_context(snap)
        sweeps = engine["detect_liquidity_sweeps"](
            snap,
            highs,
            lows,
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
        and pd.Timestamp(start_time) < pd.Timestamp(s["time"]) <= pd.Timestamp(event_time)
    ]


def displacement_after_sweep(df, sweep, direction, event_time):
    if sweep is None:
        return None

    snap = df[df["time"] <= pd.Timestamp(event_time)].copy()
    try:
        d = engine["detect_displacement"](snap, sweep, direction)
    except Exception:
        return None

    if not d.get("confirmed"):
        return None

    dt = d.get("time")
    if dt is None or pd.Timestamp(dt) > pd.Timestamp(event_time):
        return None
    return d


def retest_after_event(df, event, direction, max_bars=12):
    level = event.get("price")
    if level is None:
        return False, None

    t = pd.Timestamp(event["time"])
    future = df[df["time"] > t].head(max_bars)

    for _, c in future.iterrows():
        hi = float(c["high"])
        lo = float(c["low"])
        close = float(c["close"])
        level = float(level)

        if direction == "BULLISH":
            if lo <= level and close > level:
                return True, c["time"]
        else:
            if hi >= level and close < level:
                return True, c["time"]

    return False, None


def first_structure_event(df, direction, anchor_time):
    events = structure_events(df, direction, after=anchor_time)
    if not events:
        return None
    return events[0]


def diagnose_15m(df, case):
    anchor = pd.Timestamp(case["step8_time"])
    direction = case["direction"]

    event = first_structure_event(df, direction, anchor)
    if event is None:
        return {
            "structure_found": False,
            "structure_time": None,
            "structure_event": None,
            "structure_level": None,
            "poi": False,
            "poi_time": None,
            "sweep": False,
            "sweep_time": None,
            "displacement": False,
            "displacement_time": None,
            "retest": False,
            "retest_time": None,
        }

    poi_ok, poi = poi_before_event(df, direction, event["time"])
    sweeps = sweeps_before_event(
        df, direction, anchor, event["time"]
    )

    displacement = None
    displacement_sweep = None
    for sw in reversed(sweeps):
        d = displacement_after_sweep(
            df, sw, direction, event["time"]
        )
        if d is not None:
            displacement = d
            displacement_sweep = sw
            break

    retest_ok, retest_time = retest_after_event(
        df, event, direction
    )

    return {
        "structure_found": True,
        "structure_time": event["time"],
        "structure_event": event.get("event"),
        "structure_level": event.get("price"),
        "poi": bool(poi_ok),
        "poi_time": poi.get("time") if poi else None,
        "sweep": bool(sweeps),
        "sweep_time": sweeps[-1].get("time") if sweeps else None,
        "displacement": displacement is not None,
        "displacement_time": displacement.get("time") if displacement else None,
        "displacement_sweep_time": (
            displacement_sweep.get("time")
            if displacement_sweep else None
        ),
        "retest": bool(retest_ok),
        "retest_time": retest_time,
    }


def diagnose_5m(df, setup, direction):
    if not setup or not setup["structure_found"]:
        return {
            "structure_found": False,
            "structure_time": None,
            "structure_event": None,
            "structure_level": None,
            "sweep": False,
            "sweep_time": None,
            "displacement": False,
            "displacement_time": None,
            "displacement_sweep_time": None,
            "retest": False,
            "retest_time": None,
        }

    anchor = pd.Timestamp(setup["structure_time"])
    event = first_structure_event(df, direction, anchor)

    if event is None:
        return {
            "structure_found": False,
            "structure_time": None,
            "structure_event": None,
            "structure_level": None,
            "sweep": False,
            "sweep_time": None,
            "displacement": False,
            "displacement_time": None,
            "displacement_sweep_time": None,
            "retest": False,
            "retest_time": None,
        }

    sweeps = sweeps_before_event(
        df, direction, anchor, event["time"]
    )

    displacement = None
    displacement_sweep = None
    for sw in reversed(sweeps):
        d = displacement_after_sweep(
            df, sw, direction, event["time"]
        )
        if d is not None:
            displacement = d
            displacement_sweep = sw
            break

    retest_ok, retest_time = retest_after_event(
        df, event, direction
    )

    return {
        "structure_found": True,
        "structure_time": event["time"],
        "structure_event": event.get("event"),
        "structure_level": event.get("price"),
        "sweep": bool(sweeps),
        "sweep_time": sweeps[-1].get("time") if sweeps else None,
        "displacement": displacement is not None,
        "displacement_time": displacement.get("time") if displacement else None,
        "displacement_sweep_time": (
            displacement_sweep.get("time")
            if displacement_sweep else None
        ),
        "retest": bool(retest_ok),
        "retest_time": retest_time,
    }


def main():
    case_rows = []
    component_rows = []

    for case in CASES:
        s8 = pd.Timestamp(case["step8_time"])

        m15 = fetch_candles(
            "Min15",
            s8 - pd.Timedelta(hours=24),
            s8 + pd.Timedelta(hours=36),
        )
        d15 = diagnose_15m(m15, case)

        m5 = None
        d5 = None
        if d15["structure_found"]:
            setup_t = pd.Timestamp(d15["structure_time"])
            m5 = fetch_candles(
                "Min5",
                setup_t - pd.Timedelta(hours=6),
                setup_t + pd.Timedelta(hours=18),
            )
            d5 = diagnose_5m(
                m5,
                d15,
                case["direction"],
            )

        if d5 is None:
            d5 = {
                "structure_found": False,
                "structure_time": None,
                "structure_event": None,
                "structure_level": None,
                "sweep": False,
                "sweep_time": None,
                "displacement": False,
                "displacement_time": None,
                "displacement_sweep_time": None,
                "retest": False,
                "retest_time": None,
            }

        row = {
            "case_id": case["case_id"],
            "step8_time": case["step8_time"],
            "direction": case["direction"],

            "15m_structure": d15["structure_found"],
            "15m_structure_time": d15["structure_time"],
            "15m_structure_event": d15["structure_event"],
            "15m_poi": d15["poi"],
            "15m_poi_time": d15["poi_time"],
            "15m_sweep": d15["sweep"],
            "15m_sweep_time": d15["sweep_time"],
            "15m_displacement": d15["displacement"],
            "15m_displacement_time": d15["displacement_time"],
            "15m_retest": d15["retest"],
            "15m_retest_time": d15["retest_time"],

            "5m_structure": d5["structure_found"],
            "5m_structure_time": d5["structure_time"],
            "5m_structure_event": d5["structure_event"],
            "5m_sweep": d5["sweep"],
            "5m_sweep_time": d5["sweep_time"],
            "5m_displacement": d5["displacement"],
            "5m_displacement_time": d5["displacement_time"],
            "5m_retest": d5["retest"],
            "5m_retest_time": d5["retest_time"],
        }
        case_rows.append(row)

        components = [
            ("15m", "structure", d15["structure_found"]),
            ("15m", "poi", d15["poi"]),
            ("15m", "sweep", d15["sweep"]),
            ("15m", "displacement", d15["displacement"]),
            ("15m", "retest", d15["retest"]),
            ("5m", "structure", d5["structure_found"]),
            ("5m", "sweep", d5["sweep"]),
            ("5m", "displacement", d5["displacement"]),
            ("5m", "retest", d5["retest"]),
        ]

        for tf, component, present in components:
            component_rows.append({
                "case_id": case["case_id"],
                "direction": case["direction"],
                "timeframe": tf,
                "component": component,
                "present": bool(present),
            })

    cases_df = pd.DataFrame(case_rows)
    components_df = pd.DataFrame(component_rows)

    summary_df = (
        components_df
        .groupby(["timeframe", "component"], as_index=False)["present"]
        .agg(["sum", "count"])
        .reset_index()
        .rename(columns={
            "sum": "cases_present",
            "count": "eligible_cases",
        })
    )
    summary_df["presence_rate_pct"] = (
        summary_df["cases_present"]
        / summary_df["eligible_cases"]
        * 100
    )

    cases_df.to_csv("step9_1_component_cases.csv", index=False)
    components_df.to_csv("step9_1_component_matrix.csv", index=False)
    summary_df.to_csv("step9_1_component_summary.csv", index=False)

    print("\n=== STEP 9.1 COMPONENT DIAGNOSTIC REPLAY ===")
    print(f"Step-8-confirmed cases: {len(CASES)}")
    print("\nComponent summary:")
    print(summary_df.to_string(index=False))
    print("\nCase details:")
    print(cases_df.to_string(index=False))
    print("\nReports:")
    print(" - step9_1_component_cases.csv")
    print(" - step9_1_component_matrix.csv")
    print(" - step9_1_component_summary.csv")


if __name__ == "__main__":
    main()
