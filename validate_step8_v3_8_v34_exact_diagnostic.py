"""
STEP 8 V3.8 — V3.4-EXACT DIAGNOSTIC

Uses the exact production_pipeline() and exact Step-7 V2 call from the
proven V3.4 validator. It then adds candle-level Step-8 diagnostics after
each anchored BOS.

Production engine is AST-loaded; live Discord runner is not executed.
Production Step-8 code is NOT modified.
"""

import ast
from pathlib import Path
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
LIMIT = 1000
WARMUP = 300


def load_engine_safely():
    source = Path(ENGINE_FILE).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=ENGINE_FILE)

    allowed = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            allowed.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            allowed.append(node)
        elif isinstance(node, ast.If):
            continue

    module = ast.Module(body=allowed, type_ignores=[])
    ast.fix_missing_locations(module)

    ns = {"__name__": "smc_engine_v38_replay", "__file__": ENGINE_FILE}
    exec(compile(module, ENGINE_FILE, "exec"), ns)

    class Engine:
        pass

    engine = Engine()
    for name, value in ns.items():
        if not name.startswith("__"):
            setattr(engine, name, value)
    return engine


def normalize_dataframe(df):
    df = df.copy()

    rename = {
        "timestamp": "time", "datetime": "time", "Date": "time",
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    }
    df = df.rename(columns={
        k: v for k, v in rename.items()
        if k in df.columns and v not in df.columns
    })

    required = ["time", "open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing columns: {missing}")

    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        n = pd.to_numeric(df["time"], errors="coerce")
        if n.notna().all():
            unit = "ms" if n.abs().median() > 10**11 else "s"
            df["time"] = pd.to_datetime(n, unit=unit)
        else:
            df["time"] = pd.to_datetime(df["time"])

    return df.dropna(
        subset=["time", "open", "high", "low", "close"]
    ).sort_values("time").reset_index(drop=True)


def fetch_candles(engine):
    fn = getattr(engine, "get_candles", None)
    if not callable(fn):
        raise RuntimeError("Production get_candles() not found.")

    df = fn(
        getattr(engine, "SYMBOL", "LTC_USDT"),
        getattr(engine, "TIMEFRAME", "Min60"),
        LIMIT,
    )
    df = normalize_dataframe(df)

    if len(df) < 100:
        raise RuntimeError(f"Only {len(df)} candles returned.")

    print("Using production fetch function: get_candles()")
    return df.tail(LIMIT).reset_index(drop=True)


# EXACT V3.4 production pipeline.
def production_pipeline(engine, snap):
    swing_highs, swing_lows = engine.find_swings(
        snap, engine.SWING_STRENGTH
    )
    events = engine.detect_historical_events(
        snap, swing_highs, swing_lows
    )
    structure = engine.get_current_structure(
        swing_highs, swing_lows, events, df=snap
    )
    bias = structure["bias"]

    liquidity = engine.analyze_liquidity(
        snap, swing_highs, swing_lows
    )
    premium_discount = engine.analyze_premium_discount(
        snap, swing_highs, swing_lows, bias
    )

    fvgs = engine.detect_fvgs(snap)
    order_blocks = engine.detect_order_blocks(snap, events)
    supply_demand = engine.detect_supply_demand(snap, events)

    preferred_pois, _ = engine.select_relevant_pois(
        fvgs,
        order_blocks,
        supply_demand,
        liquidity["current_price"],
        bias,
        premium_discount=premium_discount,
        events=events,
        df=snap,
    )

    all_pois = (
        [{**x, "category": "FVG"} for x in fvgs]
        + [{**x, "category": "ORDER BLOCK"} for x in order_blocks]
        + [{**x, "category": "SUPPLY/DEMAND"} for x in supply_demand]
    )

    sweeps = engine.detect_liquidity_sweeps(
        snap,
        swing_highs,
        swing_lows,
        preferred_pois=preferred_pois,
        all_pois=all_pois,
    )

    step5 = engine.get_step5_status(
        sweeps,
        bias,
        preferred_pois=preferred_pois,
        events=events,
    )

    step6 = engine.get_step6_status(
        snap,
        step5,
        bias,
    )

    step7 = engine.get_step7_status(
        snap,
        step5,
        step6,
    )

    return step5, step6, step7


def candle_diag(row, level, direction):
    h, l, c, o = map(float, [
        row["high"], row["low"], row["close"], row["open"]
    ])
    rng = max(h - l, 1e-12)

    if direction == "BULLISH":
        touch = l <= level
        close_back = c >= level
        rejection = max(0.0, (c - l) / rng)
        penetration = max(0.0, (level - l) / level * 100)
    else:
        touch = h >= level
        close_back = c <= level
        rejection = max(0.0, (h - c) / rng)
        penetration = max(0.0, (h - level) / level * 100)

    distance = abs(c - level) / level * 100
    tolerance = float(engine.STEP8_RETEST_TOLERANCE_PCT)
    min_rej = float(engine.STEP8_MIN_REJECTION_RATIO)

    return {
        "touch": touch,
        "close_back": close_back,
        "near_level": distance <= tolerance,
        "manual_confirmed": touch and close_back and rejection >= min_rej,
        "rejection_ratio": rejection,
        "penetration_pct": penetration,
        "close_distance_pct": distance,
        "body": abs(c - o),
        "range": rng,
    }


def main():
    global engine
    engine = load_engine_safely()
    df = fetch_candles(engine)

    print(f"Downloaded {len(df)} production-source candles")
    print("Discord/live runner was NOT executed.")

    if len(df) <= WARMUP:
        raise RuntimeError("Not enough candles for replay.")

    closed = df.iloc[:-1].copy().reset_index(drop=True)

    cases = {}
    snapshots = []
    candle_rows = []
    errors = 0

    # EXACT V3.4 lifecycle discovery/tracking.
    for end in range(WARMUP, len(closed)):
        snap = closed.iloc[:end].copy().reset_index(drop=True)
        asof = snap.iloc[-1]["time"]

        try:
            step5, step6, _live_step7 = production_pipeline(engine, snap)
        except Exception as exc:
            errors += 1
            snapshots.append({"asof": str(asof), "error": str(exc)})
            continue

        displacement = step6.get("displacement") if step6 else None

        if displacement and displacement.get("confirmed"):
            candle = displacement.get("candle") or {}
            sweep = step5.get("latest") if step5 else None

            if sweep is not None and candle.get("time") is not None:
                key = (
                    str(sweep.get("time")),
                    str(candle.get("time")),
                    displacement.get("direction"),
                )

                if key not in cases:
                    cases[key] = {
                        "case_id": len(cases) + 1,
                        "sweep_time": key[0],
                        "displacement_time": key[1],
                        "direction": key[2],
                        "first_seen_asof": str(asof),
                        "step7_confirmed": False,
                        "step7_time": None,
                        "broken_level": None,
                        "step8_confirmed": False,
                        "step8_time": None,
                        "_sweep": sweep.copy(),
                        "_displacement": displacement.copy(),
                    }

        for case in cases.values():
            if case["step7_confirmed"]:
                continue

            try:
                # EXACT V3.4 Step-7 V2 invocation.
                sb = engine.detect_post_displacement_structure_break_v2(
                    snap,
                    case["_sweep"],
                    case["_displacement"],
                )
            except Exception as exc:
                case["step7_error"] = str(exc)
                continue

            if sb.get("confirmed"):
                case["step7_confirmed"] = True
                case["step7_time"] = str(sb.get("break_time"))
                case["broken_level"] = sb.get("broken_level")

                # Keep the exact anchored Step-7 object.
                anchored_step7 = {
                    "status": sb.get("status"),
                    "confirmed": True,
                    "structure_break": sb,
                }
                case["_step7"] = anchored_step7

        snapshots.append({
            "asof": str(asof),
            "step5_active": bool(step5.get("latest")) if step5 else False,
            "step6_confirmed": bool(
                displacement and displacement.get("confirmed")
            ),
            "anchored_bos_cases": sum(
                1 for c in cases.values() if c["step7_confirmed"]
            ),
        })

    # Now perform Step-8 diagnostics from the exact anchored BOS cases.
    for case in cases.values():
        if not case["step7_confirmed"]:
            continue

        bos_time = pd.to_datetime(case["step7_time"])
        level = float(case["broken_level"])
        direction = str(
            case["direction"] or ""
        ).upper()

        post_bos = closed[closed["time"] > bos_time].head(
            int(engine.STEP8_LOOKAHEAD) + 4
        )

        first_touch = None
        first_close_back = None
        first_manual = None
        production_confirmed = None
        production_status = None

        for _, row in post_bos.iterrows():
            d = candle_diag(row, level, direction)

            if d["touch"] and first_touch is None:
                first_touch = row["time"]
            if d["close_back"] and first_close_back is None:
                first_close_back = row["time"]
            if d["manual_confirmed"] and first_manual is None:
                first_manual = row["time"]

            # Production Step 8 on THIS closed snapshot using the anchored
            # production Step-7 object. No future candles are included.
            try:
                snap = closed[closed["time"] <= row["time"]].copy()
                s8 = engine.get_step8_status(
                    snap,
                    case["_step7"],
                )

                confirmed = bool(
                    s8.get("confirmed")
                    or s8.get("retest_confirmed")
                    or s8.get("entry_ready")
                )
                status = (
                    s8.get("status")
                    or s8.get("state")
                    or s8.get("sequence")
                )

                if confirmed and production_confirmed is None:
                    production_confirmed = row["time"]
                    production_status = status

            except Exception as exc:
                status = f"ERROR: {exc}"

            candle_rows.append({
                "case_id": case["case_id"],
                "displacement_time": case["displacement_time"],
                "bos_time": bos_time,
                "bos_level": level,
                "direction": direction,
                "candle_time": row["time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                **d,
                "production_step8_confirmed_now": (
                    production_confirmed == row["time"]
                    if production_confirmed is not None else False
                ),
                "production_step8_status_now": status,
            })

            if production_confirmed is not None:
                break

        case["first_touch_time"] = first_touch
        case["first_close_back_time"] = first_close_back
        case["first_manual_retest_time"] = first_manual
        case["production_step8_confirmed_time"] = production_confirmed
        case["production_step8_status"] = production_status

        if production_confirmed is not None:
            case["diagnosis"] = "STEP8_CONFIRMED"
        elif first_manual is not None:
            case["diagnosis"] = "MANUAL_RETEST_BUT_PRODUCTION_WAIT"
        elif first_touch is None:
            case["diagnosis"] = "NO_TOUCH"
        elif first_close_back is None:
            case["diagnosis"] = "TOUCH_WITHOUT_CLOSE_BACK"
        else:
            case["diagnosis"] = "CLOSE_BACK_BUT_REJECTION_RULE_NOT_MET"

    public = []
    for c in cases.values():
        public.append({
            k: v for k, v in c.items()
            if not k.startswith("_")
        })

    cases_df = pd.DataFrame(public)
    snapshots_df = pd.DataFrame(snapshots)
    candles_df = pd.DataFrame(candle_rows)

    cases_df.to_csv("step8_v3_8_cases.csv", index=False)
    snapshots_df.to_csv("step8_v3_8_snapshots.csv", index=False)
    candles_df.to_csv("step8_v3_8_candles.csv", index=False)

    bos_count = int(cases_df["step7_confirmed"].sum()) if not cases_df.empty else 0
    s8_count = (
        int(cases_df["production_step8_confirmed_time"].notna().sum())
        if not cases_df.empty and "production_step8_confirmed_time" in cases_df
        else 0
    )

    print("\n=== STEP 8 V3.8 SUMMARY ===")
    print(f"Candles loaded: {len(df)}")
    print(f"Snapshots processed: {len(snapshots)}")
    print(f"Engine errors: {errors}")
    print(f"Unique Step 6 cases: {len(cases_df)}")
    print(f"Cases with Step 7 V2 BOS: {bos_count}")
    print(f"Diagnostic post-BOS candles: {len(candles_df)}")
    print(f"Production Step 8 confirmations: {s8_count}")

    for _, row in cases_df.iterrows():
        print(
            f"Case {int(row['case_id'])}: "
            f"Step7={'BOS' if row['step7_confirmed'] else 'WAIT'} | "
            f"Step8={'CONFIRMED' if pd.notna(row.get('production_step8_confirmed_time')) else 'WAIT'} | "
            f"Disp={row['displacement_time']} | "
            f"Diagnosis={row.get('diagnosis')}"
        )

    print("\nReports:")
    print(" - step8_v3_8_cases.csv")
    print(" - step8_v3_8_snapshots.csv")
    print(" - step8_v3_8_candles.csv")


if __name__ == "__main__":
    main()
