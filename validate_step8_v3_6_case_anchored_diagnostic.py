# STEP 8 V3.6 — CASE-ANCHORED DIAGNOSTIC REPLAY
#
# Purpose:
#   Reuse the proven V3.4 Step-6 case discovery + anchored Step-7 V2 BOS path.
#   Then inspect every closed candle after each anchored BOS for Step-8 retest
#   conditions, without changing the production engine.
#
# Production engine is loaded with AST definitions only, so its live Discord
# runner is NOT executed.

from pathlib import Path
import ast
import copy
import traceback
import pandas as pd
import requests


ENGINE_FILE = Path("smc_engine_step8_full.py")
SYMBOL = "LTC_USDT"
TIMEFRAME = "Min60"
LIMIT = 1000

CASE_CSV = "step8_v3_6_cases.csv"
CANDLE_CSV = "step8_v3_6_candles.csv"


def load_engine_definitions(path):
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    kept = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            kept.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            kept.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kept.append(node)

    module = ast.Module(body=kept, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {"__name__": "smc_engine_step8_full_replay"}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


engine = load_engine_definitions(ENGINE_FILE)


def get_production_candles():
    fn = engine.get("get_candles")
    if not callable(fn):
        raise RuntimeError("Production get_candles() was not found.")

    df = fn(
        engine.get("SYMBOL", SYMBOL),
        engine.get("TIMEFRAME", TIMEFRAME),
        LIMIT,
    )

    if not isinstance(df, pd.DataFrame):
        raise RuntimeError("Production get_candles() did not return a DataFrame.")

    required = ["time", "open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing candle columns: {missing}")

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["time", "open", "high", "low", "close"])
    return df.sort_values("time").reset_index(drop=True)


def find_swings(df):
    return engine["find_swings"](
        df,
        strength=engine.get("SWING_STRENGTH", 2),
    )


def production_bias(df):
    highs, lows = find_swings(df)
    return engine["determine_initial_bias"](highs, lows)


def run_step5_step6(df):
    highs, lows = find_swings(df)
    bias = engine["determine_initial_bias"](highs, lows)

    events = engine["detect_historical_events"](
        df,
        (highs, lows),
        bias,
    )

    pois = engine["identify_pois"](
        df,
        bias=bias,
        events=events,
    )

    sweeps = engine["detect_liquidity_sweeps"](
        df,
        bias=bias,
        preferred_pois=pois,
    )

    step5 = engine["get_step5_status"](
        sweeps,
        bias,
        preferred_pois=pois,
        events=events,
    )

    step6 = engine["get_step6_status"](
        df,
        step5,
        bias=bias,
        preferred_pois=pois,
        events=events,
    )

    return bias, events, pois, sweeps, step5, step6


def normalize_step6_confirmation(step6):
    if not isinstance(step6, dict):
        return None

    # Production wrappers may expose the displacement under different keys.
    disp = (
        step6.get("displacement")
        or step6.get("confirmed_displacement")
        or step6.get("confirmation")
    )

    if not isinstance(disp, dict):
        return None

    candle = disp.get("candle")
    if not isinstance(candle, dict):
        candle = {}

    t = (
        disp.get("time")
        or candle.get("time")
        or disp.get("timestamp")
    )

    if t is None:
        return None

    return {
        "time": pd.to_datetime(t),
        "direction": disp.get("direction"),
        "score": disp.get("score"),
        "body_ratio": disp.get("body_ratio"),
        "range_ratio": disp.get("range_ratio"),
        "body_pct": disp.get("body_pct"),
        "close_location": disp.get("close_location"),
        "raw": disp,
    }


def get_active_sweep(step5, step6=None):
    if not isinstance(step5, dict):
        return None

    for key in ("active_sweep", "sweep", "confirmed_sweep"):
        value = step5.get(key)
        if isinstance(value, dict):
            return value

    # Some production wrappers place it inside sequence/status data.
    for key in ("status", "sequence", "active"):
        value = step5.get(key)
        if isinstance(value, dict):
            for sub in ("sweep", "active_sweep"):
                if isinstance(value.get(sub), dict):
                    return value[sub]

    return None


def get_step7_v2(df, sweep, displacement):
    """
    Call the production Step-7 V2 implementation using signature inspection.
    This avoids hard-coding one wrapper shape.
    """
    fn = engine.get("get_step7_status")
    if not callable(fn):
        raise RuntimeError("Production get_step7_status() not found.")

    import inspect
    sig = inspect.signature(fn)
    params = list(sig.parameters)

    candidates = {
        "df": df,
        "data": df,
        "candles": df,
        "sweep": sweep,
        "active_sweep": sweep,
        "displacement": displacement,
        "confirmed_displacement": displacement,
    }

    kwargs = {}
    for p in params:
        if p in candidates:
            kwargs[p] = candidates[p]

    try:
        return fn(**kwargs)
    except TypeError:
        # Fallback to common production positional form.
        attempts = [
            (df, sweep, displacement),
            (df, displacement, sweep),
            (df, {"sweep": sweep, "displacement": displacement}),
        ]
        last = None
        for args in attempts:
            try:
                return fn(*args)
            except TypeError as e:
                last = e
        raise last


def extract_bos(step7):
    if not isinstance(step7, dict):
        return None

    # Production status may expose the break under several names.
    for key in ("structure_break", "bos", "confirmed_bos", "break"):
        value = step7.get(key)
        if isinstance(value, dict):
            t = value.get("time") or value.get("timestamp") or value.get("candle_time")
            level = (
                value.get("broken_level")
                or value.get("level")
                or value.get("price")
            )
            direction = value.get("direction")
            if t is not None and level is not None:
                return {
                    "time": pd.to_datetime(t),
                    "level": float(level),
                    "direction": direction,
                    "raw": value,
                }

    # Nested status containers.
    for key in ("status", "result", "confirmation"):
        value = step7.get(key)
        if isinstance(value, dict):
            found = extract_bos(value)
            if found:
                return found

    # Some wrappers use a boolean plus separate fields.
    if step7.get("confirmed") and step7.get("break_time") is not None:
        level = step7.get("broken_level") or step7.get("level")
        if level is not None:
            return {
                "time": pd.to_datetime(step7["break_time"]),
                "level": float(level),
                "direction": step7.get("direction"),
                "raw": step7,
            }

    return None


def candle_metrics(row, level, direction):
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    open_ = float(row["open"])

    rng = max(high - low, 1e-12)
    body = abs(close - open_)

    if direction == "BULLISH":
        touch = low <= level
        close_back = close >= level
        penetration_pct = max(0.0, (level - low) / level * 100.0)
        close_distance_pct = abs(close - level) / level * 100.0
        rejection_ratio = max(0.0, (close - low) / rng)
    else:
        touch = high >= level
        close_back = close <= level
        penetration_pct = max(0.0, (high - level) / level * 100.0)
        close_distance_pct = abs(close - level) / level * 100.0
        rejection_ratio = max(0.0, (high - close) / rng)

    return {
        "touch": bool(touch),
        "close_back": bool(close_back),
        "rejection_ratio": float(rejection_ratio),
        "penetration_pct": float(penetration_pct),
        "close_distance_pct": float(close_distance_pct),
        "body": float(body),
        "range": float(rng),
    }


def manual_retest(row, level, direction):
    m = candle_metrics(row, level, direction)
    tolerance = float(engine.get("STEP8_RETEST_TOLERANCE_PCT", 0.15))
    min_rejection = float(engine.get("STEP8_MIN_REJECTION_RATIO", 0.25))

    near = m["close_distance_pct"] <= tolerance
    confirmed = m["touch"] and m["close_back"] and (
        m["rejection_ratio"] >= min_rejection
    )

    return m, near, confirmed


print("=== STEP 8 V3.6 CASE-ANCHORED DIAGNOSTIC REPLAY ===")

df = get_production_candles()
print(f"Candles loaded: {len(df)}")
print("Using production get_candles(): yes")
print("Discord/live runner: NOT executed")

# Build snapshots exactly from closed candles.
# Exclude the final forming candle.
closed = df.iloc[:-1].reset_index(drop=True)

# First discover Step-6 cases using the same production path that worked in V3.4.
step6_cases = []
seen = set()
errors = 0

for i in range(100, len(closed)):
    snap = closed.iloc[: i + 1].copy()

    try:
        bias, events, pois, sweeps, step5, step6 = run_step5_step6(snap)
        conf = normalize_step6_confirmation(step6)

        if conf is None:
            continue

        disp_time = conf["time"]

        sweep = get_active_sweep(step5, step6)
        if sweep is None:
            # Search raw sweeps for the latest sweep that predates displacement.
            if isinstance(sweeps, list):
                candidates = []
                for s in sweeps:
                    if not isinstance(s, dict):
                        continue
                    st = s.get("time") or s.get("timestamp")
                    if st is None:
                        continue
                    st = pd.to_datetime(st)
                    if st < disp_time:
                        candidates.append((st, s))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    sweep = candidates[-1][1]

        key = str(disp_time)
        if key in seen:
            continue

        seen.add(key)
        step6_cases.append({
            "snapshot_index": i,
            "snapshot_time": snap.iloc[-1]["time"],
            "displacement": conf,
            "sweep": sweep,
            "bias": bias,
        })

    except Exception:
        errors += 1
        continue

print(f"Unique Step 6 cases: {len(step6_cases)}")
print(f"Discovery errors: {errors}")

case_rows = []
candle_rows = []

# For every proven Step-6 case, anchor the exact displacement and then
# evaluate production Step-7 on subsequent snapshots.
for case_no, case in enumerate(step6_cases, start=1):
    disp = case["displacement"]
    disp_time = pd.to_datetime(disp["time"])
    sweep = case["sweep"]

    if sweep is None:
        print(f"CASE {case_no}: no sweep anchor; skipped")
        continue

    bos = None
    bos_snapshot_time = None
    step7_raw = None

    # Only inspect snapshots after the displacement candle.
    later = closed[closed["time"] > disp_time].copy()

    for _, row in later.iterrows():
        snap = closed[closed["time"] <= row["time"]].copy()
        try:
            step7_raw = get_step7_v2(snap, sweep, disp)
            candidate = extract_bos(step7_raw)

            if candidate is not None:
                bos = candidate
                bos_snapshot_time = row["time"]
                break
        except Exception:
            continue

    case_record = {
        "case_no": case_no,
        "sweep_time": (
            pd.to_datetime(
                sweep.get("time") or sweep.get("timestamp")
            ) if isinstance(sweep, dict)
            and (sweep.get("time") or sweep.get("timestamp")) is not None
            else None
        ),
        "displacement_time": disp_time,
        "displacement_direction": disp.get("direction"),
        "displacement_score": disp.get("score"),
        "step7_bos": bool(bos),
        "bos_time": bos["time"] if bos else None,
        "bos_snapshot_time": bos_snapshot_time,
        "bos_level": bos["level"] if bos else None,
        "bos_direction": bos["direction"] if bos else None,
        "step8_production_confirmed": False,
        "step8_production_status": "NO_BOS",
        "manual_retest_confirmed": False,
        "first_touch_time": None,
        "first_close_back_time": None,
        "first_manual_candidate_time": None,
        "step8_rejection_reason": None,
    }

    if bos is None:
        case_rows.append(case_record)
        continue

    # Inspect every closed candle after BOS.
    post_bos = closed[closed["time"] > bos["time"]].copy()

    first_touch = None
    first_close_back = None
    first_manual = None
    production_confirmed = None
    production_status = None

    for _, row in post_bos.iterrows():
        m, near, manual_confirmed = manual_retest(
            row,
            bos["level"],
            bos["direction"] or disp.get("direction"),
        )

        # Run the actual production Step 8 wrapper on this snapshot.
        try:
            snap = closed[closed["time"] <= row["time"]].copy()

            # Step 7 result is re-evaluated at this snapshot with the anchored
            # sweep/displacement. This prevents earlier snapshots from using
            # future information.
            s7 = get_step7_v2(snap, sweep, disp)
            b = extract_bos(s7)
            if b is not None:
                s8 = engine["get_step8_status"](snap, s7)
                if isinstance(s8, dict):
                    production_status = (
                        s8.get("status")
                        or s8.get("state")
                        or s8.get("sequence")
                    )
                    if (
                        s8.get("confirmed")
                        or s8.get("retest_confirmed")
                        or s8.get("entry_ready")
                    ):
                        production_confirmed = row["time"]
        except Exception:
            pass

        if m["touch"] and first_touch is None:
            first_touch = row["time"]
        if m["close_back"] and first_close_back is None:
            first_close_back = row["time"]
        if manual_confirmed and first_manual is None:
            first_manual = row["time"]

        candle_rows.append({
            "case_no": case_no,
            "candle_time": row["time"],
            "bos_time": bos["time"],
            "bos_level": bos["level"],
            "direction": bos["direction"] or disp.get("direction"),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "touch": m["touch"],
            "close_back": m["close_back"],
            "near_level": near,
            "manual_retest_confirmed": manual_confirmed,
            "rejection_ratio": m["rejection_ratio"],
            "penetration_pct": m["penetration_pct"],
            "close_distance_pct": m["close_distance_pct"],
            "body": m["body"],
            "range": m["range"],
            "production_step8_confirmed": (
                production_confirmed == row["time"]
                if production_confirmed is not None
                else False
            ),
            "production_step8_status": production_status,
        })

        if production_confirmed is not None:
            break

    case_record["first_touch_time"] = first_touch
    case_record["first_close_back_time"] = first_close_back
    case_record["first_manual_candidate_time"] = first_manual
    case_record["manual_retest_confirmed"] = first_manual is not None
    case_record["step8_production_confirmed"] = production_confirmed is not None
    case_record["step8_production_status"] = (
        production_status if production_status is not None else "WAITING"
    )

    if production_confirmed is None:
        if first_manual is not None:
            case_record["step8_rejection_reason"] = (
                "MANUAL_RETEST_EXISTS_BUT_PRODUCTION_STEP8_DID_NOT_CONFIRM"
            )
        elif first_touch is None:
            case_record["step8_rejection_reason"] = "NO_TOUCH_AFTER_BOS"
        elif first_close_back is None:
            case_record["step8_rejection_reason"] = "TOUCH_WITHOUT_CLOSE_BACK"
        else:
            case_record["step8_rejection_reason"] = "CLOSE_BACK_BUT_MANUAL_REJECTION_RULE_NOT_MET"

    case_rows.append(case_record)

cases_df = pd.DataFrame(case_rows)
candles_df = pd.DataFrame(candle_rows)

if cases_df.empty:
    cases_df = pd.DataFrame(columns=[
        "case_no", "sweep_time", "displacement_time",
        "displacement_direction", "displacement_score",
        "step7_bos", "bos_time", "bos_snapshot_time", "bos_level",
        "bos_direction", "step8_production_confirmed",
        "step8_production_status", "manual_retest_confirmed",
        "first_touch_time", "first_close_back_time",
        "first_manual_candidate_time", "step8_rejection_reason",
    ])

if candles_df.empty:
    candles_df = pd.DataFrame(columns=[
        "case_no", "candle_time", "bos_time", "bos_level", "direction",
        "open", "high", "low", "close", "touch", "close_back",
        "near_level", "manual_retest_confirmed", "rejection_ratio",
        "penetration_pct", "close_distance_pct", "body", "range",
        "production_step8_confirmed", "production_step8_status",
    ])

cases_df.to_csv(CASE_CSV, index=False)
candles_df.to_csv(CANDLE_CSV, index=False)

print("=== V3.6 SUMMARY ===")
print(f"Step 6 cases: {len(step6_cases)}")
print(f"Step 7 BOS cases: {int(cases_df['step7_bos'].sum()) if len(cases_df) else 0}")
print(
    "Manual retest candidates: "
    f"{int(cases_df['manual_retest_confirmed'].sum()) if len(cases_df) else 0}"
)
print(
    "Production Step 8 confirmations: "
    f"{int(cases_df['step8_production_confirmed'].sum()) if len(cases_df) else 0}"
)
print(f"Cases report: {CASE_CSV}")
print(f"Candles report: {CANDLE_CSV}")
