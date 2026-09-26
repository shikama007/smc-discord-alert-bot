# STEP 8 V3.7 — FIXED CASE REPLAY
#
# Uses the six Step-6 cases already proven by V3.4.
# It does NOT rediscover Step-6 cases.
# It replays each exact displacement case, obtains production Step-7 V2 BOS,
# then inspects every subsequent closed candle for Step-8 retest conditions.
#
# Production engine is loaded with AST definitions only; live Discord runner
# is not executed.

from pathlib import Path
import ast
import inspect
import pandas as pd


ENGINE_FILE = Path("smc_engine_step8_full.py")
LIMIT = 1000

CASES_CSV = "step8_v3_7_cases.csv"
CANDLES_CSV = "step8_v3_7_candles.csv"

# Proven V3.4 Step-6 displacement anchors.
FIXED_CASES = [
    {"case_no": 1, "displacement_time": "2026-09-07 13:00:00"},
    {"case_no": 2, "displacement_time": "2026-09-19 21:00:00"},
    {"case_no": 3, "displacement_time": "2026-09-20 02:00:00"},
    {"case_no": 4, "displacement_time": "2026-09-24 04:00:00"},
    {"case_no": 5, "displacement_time": "2026-09-24 13:00:00"},
    {"case_no": 6, "displacement_time": "2026-09-25 22:00:00"},
]


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

    ns = {"__name__": "smc_engine_step8_v3_7_replay"}
    exec(compile(module, str(path), "exec"), ns)
    return ns


engine = load_engine_definitions(ENGINE_FILE)


def get_candles():
    fn = engine.get("get_candles")
    if not callable(fn):
        raise RuntimeError("Production get_candles() not found.")

    df = fn(
        engine.get("SYMBOL", "LTC_USDT"),
        engine.get("TIMEFRAME", "Min60"),
        LIMIT,
    )

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df.dropna(
        subset=["time", "open", "high", "low", "close"]
    ).sort_values("time").reset_index(drop=True)


def find_swings(df):
    return engine["find_swings"](
        df,
        strength=engine.get("SWING_STRENGTH", 2),
    )


def build_production_context(df):
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

    return bias, events, pois, sweeps


def normalize_sweep(s):
    if not isinstance(s, dict):
        return None

    t = s.get("time") or s.get("timestamp") or s.get("candle_time")
    if t is None:
        return None

    out = dict(s)
    out["time"] = pd.to_datetime(t)
    return out


def select_sweep(sweeps, displacement_time, direction=None):
    candidates = []

    if isinstance(sweeps, dict):
        values = []
        for v in sweeps.values():
            if isinstance(v, list):
                values.extend(v)
            elif isinstance(v, dict):
                values.append(v)
        sweeps = values

    if not isinstance(sweeps, list):
        return None

    for s in sweeps:
        s = normalize_sweep(s)
        if s is None or s["time"] >= displacement_time:
            continue

        sd = str(s.get("direction", "")).upper()
        dd = str(direction or "").upper()

        if dd and sd and sd != dd:
            continue

        candidates.append(s)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["time"])
    return candidates[-1]


def get_displacement_for_snapshot(df, displacement_time):
    """
    Reconstruct the exact displacement candle from the production Step-6
    detector, but do not require Step-6 discovery to succeed at replay time.
    """
    try:
        bias, events, pois, sweeps = build_production_context(df)

        # Rebuild Step 5 and Step 6 using production wrappers.
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

        if isinstance(step6, dict):
            disp = (
                step6.get("displacement")
                or step6.get("confirmed_displacement")
                or step6.get("confirmation")
            )
            if isinstance(disp, dict):
                t = disp.get("time")
                candle = disp.get("candle")
                if t is None and isinstance(candle, dict):
                    t = candle.get("time")
                if t is not None and pd.to_datetime(t) == displacement_time:
                    return disp

    except Exception:
        pass

    # Fallback: exact candle data remains anchored; Step-7 receives the
    # production-style displacement dictionary.
    row = df[df["time"] == displacement_time]
    if row.empty:
        return None

    r = row.iloc[-1]
    body = abs(float(r["close"]) - float(r["open"]))
    rng = max(float(r["high"]) - float(r["low"]), 1e-12)

    direction = "BULLISH" if float(r["close"]) > float(r["open"]) else "BEARISH"

    return {
        "time": displacement_time,
        "direction": direction,
        "candle": {
            "time": displacement_time,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        },
        "body_ratio": None,
        "range_ratio": None,
        "body_pct": body / max(abs(float(r["close"])), 1e-12) * 100.0,
        "close_location": (
            (float(r["close"]) - float(r["low"])) / rng
            if direction == "BULLISH"
            else (float(r["high"]) - float(r["close"])) / rng
        ),
        "score": None,
    }


def get_step7(df, sweep, displacement):
    fn = engine.get("get_step7_status")
    if not callable(fn):
        raise RuntimeError("Production get_step7_status() not found.")

    sig = inspect.signature(fn)
    candidates = {
        "df": df,
        "data": df,
        "candles": df,
        "sweep": sweep,
        "active_sweep": sweep,
        "displacement": displacement,
        "confirmed_displacement": displacement,
    }

    kwargs = {p: candidates[p] for p in sig.parameters if p in candidates}

    try:
        return fn(**kwargs)
    except TypeError:
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

    keys = (
        "structure_break",
        "bos",
        "confirmed_bos",
        "break",
        "structure_break_event",
    )

    for key in keys:
        value = step7.get(key)
        if not isinstance(value, dict):
            continue

        t = value.get("time") or value.get("timestamp") or value.get("candle_time")
        level = (
            value.get("broken_level")
            or value.get("level")
            or value.get("price")
        )

        if t is not None and level is not None:
            return {
                "time": pd.to_datetime(t),
                "level": float(level),
                "direction": value.get("direction"),
                "raw": value,
            }

    for key in ("status", "result", "confirmation"):
        value = step7.get(key)
        if isinstance(value, dict):
            found = extract_bos(value)
            if found:
                return found

    return None


def retest_metrics(row, level, direction):
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    open_ = float(row["open"])

    rng = max(high - low, 1e-12)

    if direction == "BULLISH":
        touch = low <= level
        close_back = close >= level
        rejection = max(0.0, (close - low) / rng)
        penetration = max(0.0, (level - low) / level * 100.0)
    else:
        touch = high >= level
        close_back = close <= level
        rejection = max(0.0, (high - close) / rng)
        penetration = max(0.0, (high - level) / level * 100.0)

    distance = abs(close - level) / level * 100.0
    tolerance = float(engine.get("STEP8_RETEST_TOLERANCE_PCT", 0.15))
    min_rejection = float(engine.get("STEP8_MIN_REJECTION_RATIO", 0.25))

    return {
        "touch": bool(touch),
        "close_back": bool(close_back),
        "near_level": bool(distance <= tolerance),
        "manual_retest_confirmed": bool(
            touch and close_back and rejection >= min_rejection
        ),
        "rejection_ratio": rejection,
        "penetration_pct": penetration,
        "close_distance_pct": distance,
        "range": rng,
        "body": abs(close - open_),
    }


print("=== STEP 8 V3.7 FIXED CASE REPLAY ===")

df = get_candles()
print(f"Candles loaded: {len(df)}")
print("Production get_candles(): YES")
print("Discord/live runner: NOT executed")

# Closed candles only.
closed = df.iloc[:-1].reset_index(drop=True)

case_rows = []
candle_rows = []

for case in FIXED_CASES:
    case_no = case["case_no"]
    displacement_time = pd.Timestamp(case["displacement_time"])

    print(f"\nCASE {case_no}: displacement={displacement_time}")

    disp_row = closed[closed["time"] == displacement_time]
    if disp_row.empty:
        print("  -> displacement candle not found in downloaded data")
        case_rows.append({
            "case_no": case_no,
            "displacement_time": displacement_time,
            "status": "DISPLACEMENT_NOT_FOUND",
        })
        continue

    # Build context only up to the displacement snapshot.
    disp_snapshot = closed[closed["time"] <= displacement_time].copy()

    try:
        bias, events, pois, sweeps = build_production_context(disp_snapshot)
    except Exception as e:
        print(f"  -> context error: {e}")
        case_rows.append({
            "case_no": case_no,
            "displacement_time": displacement_time,
            "status": "CONTEXT_ERROR",
            "error": str(e),
        })
        continue

    displacement = get_displacement_for_snapshot(
        disp_snapshot,
        displacement_time,
    )

    if displacement is None:
        print("  -> displacement reconstruction failed")
        case_rows.append({
            "case_no": case_no,
            "displacement_time": displacement_time,
            "status": "DISPLACEMENT_RECONSTRUCTION_FAILED",
        })
        continue

    direction = str(
        displacement.get("direction")
        or "UNKNOWN"
    ).upper()

    sweep = select_sweep(
        sweeps,
        displacement_time,
        direction=direction,
    )

    if sweep is None:
        print("  -> no pre-displacement sweep anchor found")
        case_rows.append({
            "case_no": case_no,
            "displacement_time": displacement_time,
            "direction": direction,
            "status": "SWEEP_NOT_FOUND",
        })
        continue

    # Step 7 is evaluated progressively after displacement.
    bos = None
    bos_snapshot = None
    step7_status = None

    later = closed[closed["time"] > displacement_time]

    for _, r in later.iterrows():
        snap = closed[closed["time"] <= r["time"]].copy()
        try:
            s7 = get_step7(snap, sweep, displacement)
            step7_status = s7
            candidate = extract_bos(s7)
            if candidate is not None:
                bos = candidate
                bos_snapshot = r["time"]
                break
        except Exception:
            continue

    if bos is None:
        print("  -> Step 7 BOS not found")
        case_rows.append({
            "case_no": case_no,
            "displacement_time": displacement_time,
            "direction": direction,
            "sweep_time": sweep.get("time"),
            "status": "NO_BOS",
        })
        continue

    print(
        f"  -> BOS {bos['time']} level={bos['level']} "
        f"direction={bos['direction'] or direction}"
    )

    bos_direction = str(
        bos.get("direction") or direction
    ).upper()

    first_touch = None
    first_close_back = None
    first_manual = None
    production_confirmed = None
    production_status = None

    # Inspect up to production Step-8 lookahead + a small diagnostic margin.
    max_candles = int(engine.get("STEP8_LOOKAHEAD", 8)) + 4
    post_bos = closed[closed["time"] > bos["time"]].head(max_candles)

    for _, r in post_bos.iterrows():
        metrics = retest_metrics(
            r,
            bos["level"],
            bos_direction,
        )

        if metrics["touch"] and first_touch is None:
            first_touch = r["time"]

        if metrics["close_back"] and first_close_back is None:
            first_close_back = r["time"]

        if metrics["manual_retest_confirmed"] and first_manual is None:
            first_manual = r["time"]

        production_status_now = None
        production_confirmed_now = False

        try:
            snap = closed[closed["time"] <= r["time"]].copy()
            s7 = get_step7(snap, sweep, displacement)
            s8 = engine["get_step8_status"](snap, s7)

            if isinstance(s8, dict):
                production_status_now = (
                    s8.get("status")
                    or s8.get("state")
                    or s8.get("sequence")
                )
                production_confirmed_now = bool(
                    s8.get("confirmed")
                    or s8.get("retest_confirmed")
                    or s8.get("entry_ready")
                )

                if production_confirmed_now and production_confirmed is None:
                    production_confirmed = r["time"]
                    production_status = production_status_now

        except Exception as e:
            production_status_now = f"ERROR: {e}"

        candle_rows.append({
            "case_no": case_no,
            "displacement_time": displacement_time,
            "bos_time": bos["time"],
            "bos_level": bos["level"],
            "direction": bos_direction,
            "candle_time": r["time"],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "touch": metrics["touch"],
            "close_back": metrics["close_back"],
            "near_level": metrics["near_level"],
            "manual_retest_confirmed": metrics["manual_retest_confirmed"],
            "rejection_ratio": metrics["rejection_ratio"],
            "penetration_pct": metrics["penetration_pct"],
            "close_distance_pct": metrics["close_distance_pct"],
            "body": metrics["body"],
            "range": metrics["range"],
            "production_step8_confirmed_now": production_confirmed_now,
            "production_step8_status_now": production_status_now,
        })

        if production_confirmed is not None:
            break

    if production_confirmed is not None:
        status = "STEP8_CONFIRMED"
    elif first_manual is not None:
        status = "MANUAL_RETEST_BUT_STEP8_WAIT"
    elif first_touch is None:
        status = "NO_TOUCH"
    elif first_close_back is None:
        status = "TOUCH_NO_CLOSE_BACK"
    else:
        status = "CLOSE_BACK_NO_MANUAL_REJECTION"

    case_rows.append({
        "case_no": case_no,
        "displacement_time": displacement_time,
        "direction": direction,
        "sweep_time": sweep.get("time"),
        "bos_time": bos["time"],
        "bos_snapshot_time": bos_snapshot,
        "bos_level": bos["level"],
        "bos_direction": bos_direction,
        "first_touch_time": first_touch,
        "first_close_back_time": first_close_back,
        "first_manual_retest_time": first_manual,
        "production_step8_confirmed_time": production_confirmed,
        "production_step8_status": production_status,
        "status": status,
    })

cases_df = pd.DataFrame(case_rows)
candles_df = pd.DataFrame(candle_rows)

cases_df.to_csv(CASES_CSV, index=False)
candles_df.to_csv(CANDLES_CSV, index=False)

print("\n=== V3.7 SUMMARY ===")
print(f"Fixed cases: {len(FIXED_CASES)}")
print(f"BOS cases: {sum(cases_df.get('bos_time', pd.Series(dtype=object)).notna()) if len(cases_df) else 0}")
print(f"Candle diagnostics: {len(candles_df)}")
print(
    "Production Step 8 confirmed: "
    f"{sum(cases_df.get('production_step8_confirmed_time', pd.Series(dtype=object)).notna()) if len(cases_df) else 0}"
)
print(f"Cases report: {CASES_CSV}")
print(f"Candles report: {CANDLES_CSV}")
