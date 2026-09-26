"""
STEP 8 V3.5.1 DIAGNOSTIC REPLAY

Diagnostic-only validator.
- Loads smc_engine_step8_full.py definitions without executing the Discord runner.
- Uses the exact production get_candles() function.
- Finds Step 6 cases.
- Anchors each Step 6 case.
- Finds Step 7 V2 BOS using the anchored sweep + displacement.
- Prints every important intermediate value.
- Inspects post-BOS closed candles for Step 8 conditions.
- Saves non-empty diagnostic CSVs whenever cases/BOS data exist.

Outputs:
  step8_v3_5_1_diagnostic_cases.csv
  step8_v3_5_1_diagnostic_candles.csv
"""

from pathlib import Path
import ast
import copy
import pandas as pd

ENGINE_FILE = Path("smc_engine_step8_full.py")
LIMIT = 1000
MIN_CANDLES = 100
MAX_POST_BOS_CANDLES = 12


def load_engine_definitions(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    keep = []

    for node in tree.body:
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Assign,
                ast.AnnAssign,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        ):
            keep.append(node)

    module = ast.Module(body=keep, type_ignores=[])
    code = compile(module, str(path), "exec")
    namespace = {
        "__file__": str(path),
        "__name__": "production_engine_definitions",
    }
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

    df = df.dropna(
        subset=["time", "open", "high", "low", "close"]
    ).sort_values("time")

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
    # Production-safe: remove final forming candle.
    snap = df.iloc[: end_index + 1].copy()

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


def normalize_displacement(displacement):
    if not isinstance(displacement, dict):
        return None

    candle = displacement.get("candle")

    if not isinstance(candle, dict):
        return None

    if candle.get("time") is None:
        return None

    result = copy.deepcopy(displacement)
    result["candle"]["time"] = str(result["candle"]["time"])

    return result


def get_step6_from_snapshot(engine, snap):
    bias = engine["determine_initial_bias"](snap)

    swings = engine["find_swings"](
        snap,
        strength=engine.get("SWING_STRENGTH", 2),
    )

    events = engine["detect_historical_events"](
        snap,
        swings,
        bias,
    )

    preferred_pois = []

    try:
        if "get_quality_pois" in engine:
            preferred_pois = engine["get_quality_pois"](
                snap,
                bias,
            )
    except Exception as exc:
        print(f"POI diagnostic warning: {type(exc).__name__}: {exc}")

    sweeps = engine["detect_liquidity_sweeps"](
        snap,
        bias,
        preferred_pois,
        events,
    )

    step5 = engine["get_step5_status"](
        sweeps,
        bias,
        preferred_pois=preferred_pois,
        events=events,
    )

    step6 = engine["get_step6_status"](
        snap,
        step5,
    )

    return bias, step5, step6


def find_step6_cases(engine, df):
    cases = []
    seen = set()

    print()
    print("=== SEARCHING STEP 6 CASES ===")

    for i in range(20, len(df)):
        snap = closed_snapshot(df, i)

        if len(snap) < MIN_CANDLES:
            continue

        try:
            bias, step5, step6 = get_step6_from_snapshot(
                engine,
                snap,
            )
        except Exception as exc:
            print(
                f"Step6 snapshot error at "
                f"{snap.iloc[-1]['time']}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        displacement = normalize_displacement(
            step6.get("displacement")
        )

        if not step6.get("confirmed") or displacement is None:
            continue

        disp_time = str(
            displacement["candle"]["time"]
        )

        if disp_time in seen:
            continue

        sweep = step5.get("latest")

        if not isinstance(sweep, dict):
            print(
                f"Step6 confirmed but no Step5 latest sweep: "
                f"{disp_time}"
            )
            continue

        case = {
            "case_id": len(cases) + 1,
            "displacement_time": disp_time,
            "displacement": copy.deepcopy(displacement),
            "sweep": copy.deepcopy(sweep),
            "bias": bias,
            "detected_snapshot_time": str(
                snap.iloc[-1]["time"]
            ),
        }

        cases.append(case)
        seen.add(disp_time)

        print(
            f"FOUND CASE {case['case_id']}: "
            f"Disp={disp_time} | "
            f"Bias={bias} | "
            f"Sweep={sweep.get('direction')} "
            f"@ {sweep.get('level')}"
        )

    print(f"STEP 6 CASES FOUND: {len(cases)}")
    return cases


def find_bos_for_case(engine, df, case):
    detector = engine.get(
        "detect_post_displacement_structure_break"
    )

    if not callable(detector):
        raise RuntimeError(
            "Production Step 7 V2 detector "
            "detect_post_displacement_structure_break "
            "was not found."
        )

    disp_time = safe_time(
        case["displacement_time"]
    )

    start = None

    for i, t in enumerate(df["time"]):
        if safe_time(t) == disp_time:
            start = i
            break

    if start is None:
        print(
            f"Case {case['case_id']}: "
            f"displacement candle not found in replay"
        )
        return None, None

    print(
        f"Case {case['case_id']}: "
        f"searching Step 7 after displacement "
        f"{case['displacement_time']}"
    )

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
        except Exception as exc:
            print(
                f"  Step7 detector error at "
                f"{snap.iloc[-1]['time']}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        if not isinstance(result, dict):
            continue

        status = str(
            result.get("status", "")
        ).upper()

        confirmed = bool(
            result.get("confirmed")
            or result.get("bos_confirmed")
            or status == "BOS"
        )

        if confirmed:
            print(
                f"  BOS FOUND at snapshot "
                f"{snap.iloc[-1]['time']}"
            )
            print(f"  BOS result: {result}")
            return copy.deepcopy(result), i

    print(
        f"  Case {case['case_id']}: "
        f"NO BOS FOUND"
    )

    return None, None


def extract_level_direction(structure_break):
    if not isinstance(structure_break, dict):
        return None, None

    level = None

    for key in (
        "broken_level",
        "break_level",
        "level",
        "price",
        "broken_price",
    ):
        if structure_break.get(key) is not None:
            level = structure_break.get(key)
            break

    direction = (
        structure_break.get("direction")
        or structure_break.get("bias")
    )

    try:
        level = float(level)
    except Exception:
        level = None

    if direction is not None:
        direction = str(direction).upper()

    return level, direction


def diagnostic_conditions(engine, candle, level, direction):
    if level is None:
        return {
            "touch": False,
            "close_back": False,
            "rejection_ratio": None,
            "rejection_pass": False,
            "manual_candidate": False,
            "reason": "NO_BOS_LEVEL",
        }

    if direction not in ("BULLISH", "BEARISH"):
        return {
            "touch": False,
            "close_back": False,
            "rejection_ratio": None,
            "rejection_pass": False,
            "manual_candidate": False,
            "reason": "NO_VALID_DIRECTION",
        }

    tolerance_pct = float(
        engine.get(
            "STEP8_RETEST_TOLERANCE_PCT",
            0.15,
        )
    )

    min_rejection = float(
        engine.get(
            "STEP8_MIN_REJECTION_RATIO",
            0.25,
        )
    )

    tolerance = level * tolerance_pct / 100.0

    zone_low = level - tolerance
    zone_high = level + tolerance

    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])
    open_price = float(candle["open"])

    candle_range = max(high - low, 1e-12)

    touch = (
        low <= zone_high
        and high >= zone_low
    )

    if direction == "BULLISH":
        close_back = close > level
        wick = min(open_price, close) - low
    else:
        close_back = close < level
        wick = high - max(open_price, close)

    rejection_ratio = max(
        0.0,
        wick,
    ) / candle_range

    rejection_pass = (
        rejection_ratio >= min_rejection
    )

    manual_candidate = (
        touch
        and close_back
        and rejection_pass
    )

    return {
        "touch": bool(touch),
        "close_back": bool(close_back),
        "rejection_ratio": rejection_ratio,
        "rejection_pass": bool(rejection_pass),
        "manual_candidate": bool(manual_candidate),
        "tolerance_pct": tolerance_pct,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "reason": "CHECKED",
    }


def production_step8(engine, snap, structure_break):
    try:
        result = engine["get_step8_status"](
            snap,
            structure_break,
        )

        if not isinstance(result, dict):
            return {}, "NON_DICT_RESULT"

        return result, ""

    except Exception as exc:
        return {}, (
            f"{type(exc).__name__}: {exc}"
        )


def main():
    if not ENGINE_FILE.exists():
        raise FileNotFoundError(
            f"Missing production engine: {ENGINE_FILE}"
        )

    engine = load_engine_definitions(
        ENGINE_FILE
    )

    df = get_production_candles(
        engine
    )

    print()
    print(
        "=== STEP 8 V3.5.1 DIAGNOSTIC REPLAY ==="
    )
    print(
        f"Candles loaded: {len(df)}"
    )

    cases = find_step6_cases(
        engine,
        df,
    )

    case_rows = []
    candle_rows = []

    for case in cases:
        print()
        print(
            f"========== CASE {case['case_id']} =========="
        )
        print(
            f"Displacement: "
            f"{case['displacement_time']}"
        )
        print(
            f"Sweep: "
            f"{case['sweep']}"
        )

        structure_break, bos_index = (
            find_bos_for_case(
                engine,
                df,
                case,
            )
        )

        level, direction = (
            extract_level_direction(
                structure_break
            )
        )

        print(
            f"BOS LEVEL: {level}"
        )
        print(
            f"BOS DIRECTION: {direction}"
        )

        production_retest = False
        manual_retest = False
        inspected = 0

        if bos_index is not None:
            bos_time = str(
                df.iloc[bos_index]["time"]
            )

            print(
                f"BOS INDEX: {bos_index}"
            )
            print(
                f"BOS SNAPSHOT: {bos_time}"
            )

            end = min(
                len(df),
                bos_index + 1 + MAX_POST_BOS_CANDLES,
            )

            for j in range(
                bos_index + 1,
                end,
            ):
                snap = closed_snapshot(
                    df,
                    j,
                )

                if len(snap) == 0:
                    continue

                candle = (
                    snap.iloc[-1]
                    .to_dict()
                )

                diag = diagnostic_conditions(
                    engine,
                    candle,
                    level,
                    direction,
                )

                prod, prod_error = (
                    production_step8(
                        engine,
                        snap,
                        structure_break,
                    )
                )

                prod_status = str(
                    prod.get(
                        "status",
                        ""
                    )
                )

                prod_confirmed = bool(
                    prod.get("confirmed")
                    or prod.get(
                        "retest_confirmed"
                    )
                    or prod_status.upper()
                    in (
                        "RETEST",
                        "CONFIRMED",
                    )
                )

                if prod_confirmed:
                    production_retest = True

                if diag["manual_candidate"]:
                    manual_retest = True

                inspected += 1

                print(
                    f"  Candle {inspected}: "
                    f"{candle['time']} | "
                    f"H={candle['high']} "
                    f"L={candle['low']} "
                    f"C={candle['close']} | "
                    f"Touch={diag['touch']} | "
                    f"CloseBack={diag['close_back']} | "
                    f"Reject={diag['rejection_ratio']} | "
                    f"RejectPass={diag['rejection_pass']} | "
                    f"Manual={diag['manual_candidate']} | "
                    f"ProdStatus={prod_status or 'N/A'}"
                )

                candle_rows.append(
                    {
                        "case_id": case["case_id"],
                        "displacement_time": case[
                            "displacement_time"
                        ],
                        "bos_time": bos_time,
                        "bos_level": level,
                        "direction": direction,
                        "post_bos_number": inspected,
                        "candle_time": str(
                            candle["time"]
                        ),
                        "open": candle["open"],
                        "high": candle["high"],
                        "low": candle["low"],
                        "close": candle["close"],
                        "zone_low": diag.get(
                            "zone_low"
                        ),
                        "zone_high": diag.get(
                            "zone_high"
                        ),
                        "touch": diag["touch"],
                        "close_back": diag[
                            "close_back"
                        ],
                        "rejection_ratio": diag[
                            "rejection_ratio"
                        ],
                        "rejection_pass": diag[
                            "rejection_pass"
                        ],
                        "manual_candidate": diag[
                            "manual_candidate"
                        ],
                        "production_step8_confirmed": prod_confirmed,
                        "production_step8_status": prod.get(
                            "status"
                        ),
                        "production_step8_error": prod_error,
                    }
                )

        else:
            bos_time = None

        if production_retest:
            diagnosis = (
                "PRODUCTION_STEP8_CONFIRMED"
            )
        elif manual_retest:
            diagnosis = (
                "MANUAL_CONDITIONS_PASS_BUT_PRODUCTION_WAIT"
            )
        elif bos_index is None:
            diagnosis = (
                "NO_STEP7_BOS"
            )
        elif inspected == 0:
            diagnosis = (
                "BOS_FOUND_BUT_NO_POST_BOS_CANDLES"
            )
        else:
            diagnosis = (
                "NO_MANUAL_RETEST_IN_12_CLOSED_CANDLES"
            )

        case_rows.append(
            {
                "case_id": case["case_id"],
                "displacement_time": case[
                    "displacement_time"
                ],
                "bos_time": bos_time,
                "bos_level": level,
                "direction": direction,
                "step7_bos_found": (
                    bos_index is not None
                ),
                "post_bos_candles_inspected": inspected,
                "manual_retest_found": manual_retest,
                "production_step8_retest_found": production_retest,
                "diagnosis": diagnosis,
            }
        )

    # Always write headers, even if no cases were found.
    case_columns = [
        "case_id",
        "displacement_time",
        "bos_time",
        "bos_level",
        "direction",
        "step7_bos_found",
        "post_bos_candles_inspected",
        "manual_retest_found",
        "production_step8_retest_found",
        "diagnosis",
    ]

    candle_columns = [
        "case_id",
        "displacement_time",
        "bos_time",
        "bos_level",
        "direction",
        "post_bos_number",
        "candle_time",
        "open",
        "high",
        "low",
        "close",
        "zone_low",
        "zone_high",
        "touch",
        "close_back",
        "rejection_ratio",
        "rejection_pass",
        "manual_candidate",
        "production_step8_confirmed",
        "production_step8_status",
        "production_step8_error",
    ]

    pd.DataFrame(
        case_rows,
        columns=case_columns,
    ).to_csv(
        "step8_v3_5_1_diagnostic_cases.csv",
        index=False,
    )

    pd.DataFrame(
        candle_rows,
        columns=candle_columns,
    ).to_csv(
        "step8_v3_5_1_diagnostic_candles.csv",
        index=False,
    )

    print()
    print("=== FINAL SUMMARY ===")
    print(
        f"Step 6 cases: {len(case_rows)}"
    )
    print(
        "Step 7 BOS: "
        f"{sum(r['step7_bos_found'] for r in case_rows)}"
    )
    print(
        "Post-BOS candles inspected: "
        f"{len(candle_rows)}"
    )
    print(
        "Manual retest candidates: "
        f"{sum(r['manual_retest_found'] for r in case_rows)}"
    )
    print(
        "Production Step 8 retests: "
        f"{sum(r['production_step8_retest_found'] for r in case_rows)}"
    )

    print()
    print("Reports:")
    print(
        " - step8_v3_5_1_diagnostic_cases.csv"
    )
    print(
        " - step8_v3_5_1_diagnostic_candles.csv"
    )


if __name__ == "__main__":
    main()
