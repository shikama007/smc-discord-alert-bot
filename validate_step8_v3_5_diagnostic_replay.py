"""
STEP 8 V3.5 DIAGNOSTIC REPLAY

Purpose:
- Reuse the production candle-fetch path and production engine definitions.
- Anchor every confirmed Step 6 case.
- Find the corresponding Step 7 V2 BOS.
- Inspect every closed candle after BOS for the Step 8 retest conditions.
- Produce candle-by-candle diagnostic CSVs without changing the production engine.

Outputs:
  step8_v3_5_diagnostic_cases.csv
  step8_v3_5_diagnostic_candles.csv
"""

from pathlib import Path
import ast
import copy
import sys
import pandas as pd

ENGINE_FILE = Path("smc_engine_step8_full.py")
LIMIT = 1000
MIN_CANDLES = 100
MAX_POST_BOS_CANDLES = 12


def load_engine_definitions(path):
    """Load production definitions without executing the live Discord runner."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    keep = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
                              ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            keep.append(node)

    module = ast.Module(body=keep, type_ignores=[])
    code = compile(module, str(path), "exec")

    namespace = {"__file__": str(path), "__name__": "production_engine_definitions"}
    exec(code, namespace)
    return namespace


def normalize_dataframe(df):
    df = df.copy()
    required = ["time", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing candle columns: {missing}")

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time")
    return df.reset_index(drop=True)


def get_production_candles(engine):
    fn = engine.get("get_candles")
    if not callable(fn):
        raise RuntimeError("Production get_candles() was not found.")

    df = fn(
        engine.get("SYMBOL", "LTC_USDT"),
        engine.get("TIMEFRAME", "Min60"),
        LIMIT,
    )
    df = normalize_dataframe(df)
    if len(df) < MIN_CANDLES:
        raise RuntimeError(f"Only {len(df)} candles downloaded.")

    print("Using production fetch function: get_candles()")
    print(f"Downloaded {len(df)} production-source candles")
    print("Discord/live runner was NOT executed.")
    return df.tail(LIMIT).reset_index(drop=True)


def closed_snapshot(df, end_index):
    """Production-safe snapshot: exclude the final forming candle."""
    snap = df.iloc[:end_index + 1].copy()
    if len(snap) > 1:
        snap = snap.iloc[:-1].copy()
    return snap.reset_index(drop=True)


def safe_time(value):
    if value is None:
        return None
    try:
        return pd.to_datetime(value)
    except Exception:
        return value


def safe_get(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_step6(engine, snap):
    """Run production Step 5/6 status on a closed-candle snapshot."""
    bias = engine["determine_initial_bias"](snap)
    swings = engine["find_swings"](
        snap,
        strength=engine.get("SWING_STRENGTH", 2),
    )

    events = engine["detect_historical_events"](snap, swings, bias)

    # Build the same major/local context used by production wrappers where possible.
    preferred_pois = []
    try:
        if "get_quality_pois" in engine:
            preferred_pois = engine["get_quality_pois"](snap, bias)
    except Exception:
        preferred_pois = []

    step5 = engine["get_step5_status"](
        engine["detect_liquidity_sweeps"](snap, bias, preferred_pois, events),
        bias,
        preferred_pois=preferred_pois,
        events=events,
    )

    step6 = engine["get_step6_status"](snap, step5)
    return bias, step5, step6


def normalize_displacement(displacement):
    if not isinstance(displacement, dict):
        return None
    candle = displacement.get("candle")
    if not isinstance(candle, dict):
        return None
    if candle.get("time") is None:
        return None
    d = copy.deepcopy(displacement)
    d["candle"]["time"] = str(d["candle"]["time"])
    return d


def find_step6_cases(engine, df):
    cases = []
    seen = set()

    for i in range(20, len(df)):
        snap = closed_snapshot(df, i)
        if len(snap) < MIN_CANDLES:
            continue

        try:
            bias, step5, step6 = extract_step6(engine, snap)
        except Exception:
            continue

        displacement = normalize_displacement(step6.get("displacement"))
        if not step6.get("confirmed") or displacement is None:
            continue

        disp_time = str(displacement["candle"]["time"])
        key = disp_time
        if key in seen:
            continue

        sweep = step5.get("latest")
        if not isinstance(sweep, dict):
            continue

        cases.append({
            "case_id": len(cases) + 1,
            "displacement_time": disp_time,
            "displacement": displacement,
            "sweep": copy.deepcopy(sweep),
            "bias": bias,
            "detected_snapshot_time": str(snap.iloc[-1]["time"]),
        })
        seen.add(key)

    return cases


def find_bos_for_case(engine, df, case):
    """
    Re-evaluate the anchored production Step 7 V2 detector on each later
    closed-candle snapshot. This avoids depending on the current Step 5/6 state.
    """
    disp_time = safe_time(case["displacement_time"])
    if disp_time is None:
        return None

    start = None
    for i, t in enumerate(df["time"]):
        if safe_time(t) == disp_time:
            start = i
            break
    if start is None:
        return None

    detector = engine.get("detect_post_displacement_structure_break")
    if not callable(detector):
        raise RuntimeError("Production Step 7 V2 detector not found.")

    for i in range(start + 1, len(df)):
        snap = closed_snapshot(df, i)
        if len(snap) < MIN_CANDLES:
            continue
        try:
            result = detector(
                snap,
                case["sweep"],
                case["displacement"],
            )
        except Exception:
            continue

        if isinstance(result, dict):
            # Accept production result shapes that indicate a confirmed BOS.
            confirmed = bool(
                result.get("confirmed")
                or result.get("bos_confirmed")
                or str(result.get("status", "")).upper() == "BOS"
            )
            if confirmed:
                return copy.deepcopy(result), i

    return None, None


def infer_level_and_direction(structure_break, displacement):
    level = None
    direction = None

    if isinstance(structure_break, dict):
        for k in ("broken_level", "break_level", "level", "price"):
            if structure_break.get(k) is not None:
                level = structure_break[k]
                break
        direction = structure_break.get("direction") or structure_break.get("bias")

    if level is None and isinstance(displacement, dict):
        level = displacement.get("broken_level")

    if direction is None and isinstance(displacement, dict):
        direction = displacement.get("direction")

    try:
        level = float(level) if level is not None else None
    except Exception:
        level = None

    return level, str(direction).upper() if direction is not None else None


def manual_retest_diagnostics(engine, candle, level, direction):
    """
    Mirror the production Step 8 observable conditions where possible.
    The output is diagnostic only; production get_step8_status remains untouched.
    """
    if level is None or direction not in ("BULLISH", "BEARISH"):
        return {
            "touch": False,
            "close_back": False,
            "rejection_ratio": None,
            "rejection_pass": False,
            "manual_candidate": False,
            "reason": "missing BOS level/direction",
        }

    tol_pct = float(engine.get("STEP8_RETEST_TOLERANCE_PCT", 0.15))
    min_rej = float(engine.get("STEP8_MIN_REJECTION_RATIO", 0.25))
    tol = level * tol_pct / 100.0

    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])
    op = float(candle["open"])
    rng = max(high - low, 1e-12)

    zone_low = level - tol
    zone_high = level + tol
    touch = (low <= zone_high and high >= zone_low)

    if direction == "BULLISH":
        close_back = close > level
        wick = min(op, close) - low
    else:
        close_back = close < level
        wick = high - max(op, close)

    rejection_ratio = max(0.0, wick) / rng
    rejection_pass = rejection_ratio >= min_rej

    return {
        "touch": bool(touch),
        "close_back": bool(close_back),
        "rejection_ratio": rejection_ratio,
        "rejection_pass": bool(rejection_pass),
        "manual_candidate": bool(touch and close_back and rejection_pass),
        "tolerance_pct": tol_pct,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "reason": "manual diagnostic",
    }


def run_step8_production(engine, snap, structure_break):
    try:
        result = engine["get_step8_status"](snap, structure_break)
        if not isinstance(result, dict):
            return {}, "non-dict result"
        return result, ""
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def main():
    if not ENGINE_FILE.exists():
        raise FileNotFoundError(f"Missing production engine: {ENGINE_FILE}")

    engine = load_engine_definitions(ENGINE_FILE)
    df = get_production_candles(engine)

    print()
    print("=== STEP 8 V3.5 DIAGNOSTIC REPLAY ===")
    print(f"Candles loaded: {len(df)}")

    cases = find_step6_cases(engine, df)
    print(f"Unique Step 6 cases: {len(cases)}")

    case_rows = []
    candle_rows = []

    for case in cases:
        found = find_bos_for_case(engine, df, case)
        if isinstance(found, tuple):
            structure_break, bos_index = found
        else:
            structure_break, bos_index = None, None

        bos_time = None
        if structure_break:
            for k in ("candle_time", "break_time", "time"):
                if structure_break.get(k) is not None:
                    bos_time = structure_break.get(k)
                    break

        level, direction = infer_level_and_direction(
            structure_break,
            case["displacement"],
        )

        production_retest_found = False
        manual_retest_found = False

        if bos_index is not None:
            # Inspect the next MAX_POST_BOS_CANDLES closed candles.
            end = min(len(df), bos_index + 1 + MAX_POST_BOS_CANDLES)
            for j in range(bos_index + 1, end):
                snap = closed_snapshot(df, j)
                if len(snap) == 0:
                    continue

                candle = snap.iloc[-1].to_dict()
                diag = manual_retest_diagnostics(engine, candle, level, direction)
                prod, prod_err = run_step8_production(engine, snap, structure_break)

                prod_confirmed = bool(
                    prod.get("confirmed")
                    or prod.get("retest_confirmed")
                    or str(prod.get("status", "")).upper() in ("RETEST", "CONFIRMED")
                )

                if prod_confirmed:
                    production_retest_found = True
                if diag["manual_candidate"]:
                    manual_retest_found = True

                candle_rows.append({
                    "case_id": case["case_id"],
                    "displacement_time": case["displacement_time"],
                    "bos_time": str(bos_time) if bos_time is not None else str(df.iloc[bos_index]["time"]),
                    "bos_level": level,
                    "direction": direction,
                    "candle_time": str(candle["time"]),
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                    "touch": diag["touch"],
                    "close_back": diag["close_back"],
                    "rejection_ratio": diag["rejection_ratio"],
                    "rejection_pass": diag["rejection_pass"],
                    "manual_candidate": diag["manual_candidate"],
                    "production_step8_confirmed": prod_confirmed,
                    "production_step8_status": prod.get("status"),
                    "production_step8_error": prod_err,
                    "zone_low": diag.get("zone_low"),
                    "zone_high": diag.get("zone_high"),
                })

        case_rows.append({
            "case_id": case["case_id"],
            "displacement_time": case["displacement_time"],
            "bos_time": str(bos_time) if bos_time is not None else None,
            "bos_level": level,
            "direction": direction,
            "step7_bos_found": bos_index is not None,
            "manual_retest_found": manual_retest_found,
            "production_step8_retest_found": production_retest_found,
            "diagnosis": (
                "STEP8_PRODUCTION_CONFIRMED"
                if production_retest_found
                else "MANUAL_CONDITIONS_PASS_BUT_PRODUCTION_WAIT"
                if manual_retest_found
                else "NO_MANUAL_RETEST_IN_POST_BOS_WINDOW"
            ),
        })

    pd.DataFrame(case_rows).to_csv("step8_v3_5_diagnostic_cases.csv", index=False)
    pd.DataFrame(candle_rows).to_csv("step8_v3_5_diagnostic_candles.csv", index=False)

    print()
    print("=== SUMMARY ===")
    print(f"Step 6 cases: {len(case_rows)}")
    print(f"Step 7 BOS: {sum(r['step7_bos_found'] for r in case_rows)}")
    print(f"Manual retest candidates: {sum(r['manual_retest_found'] for r in case_rows)}")
    print(f"Production Step 8 retests: {sum(r['production_step8_retest_found'] for r in case_rows)}")

    for r in case_rows:
        print(
            f"Case {r['case_id']}: "
            f"BOS={'YES' if r['step7_bos_found'] else 'NO'} | "
            f"ManualRetest={'YES' if r['manual_retest_found'] else 'NO'} | "
            f"ProdStep8={'YES' if r['production_step8_retest_found'] else 'NO'} | "
            f"{r['diagnosis']}"
        )

    print()
    print("Reports:")
    print(" - step8_v3_5_diagnostic_cases.csv")
    print(" - step8_v3_5_diagnostic_candles.csv")


if __name__ == "__main__":
    main()
