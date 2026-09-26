"""
Step 7 V3.2 Safe Lifecycle Replay Validator

Important:
- Reuses the production engine's own candle-fetch function when available.
- Does NOT execute the production engine's live/Discord runner.
- Tracks each confirmed Step 6 case forward through later closed-candle snapshots.
"""

import ast
from pathlib import Path
import requests
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
LIMIT = 1000
WARMUP = 300


def load_engine_safely():
    """Load definitions from the production engine without running its live runner."""
    source = Path(ENGINE_FILE).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=ENGINE_FILE)

    allowed = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            allowed.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            allowed.append(node)
        elif isinstance(node, ast.If):
            # Skip module-level __main__ / executable runner blocks.
            continue

    module = ast.Module(body=allowed, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {
        "__name__": "smc_engine_replay",
        "__file__": ENGINE_FILE,
    }
    exec(compile(module, ENGINE_FILE, "exec"), namespace)

    class Engine:
        pass

    engine = Engine()
    for name, value in namespace.items():
        if not name.startswith("__"):
            setattr(engine, name, value)
    return engine


def normalize_dataframe(df):
    """Normalize whatever the production fetch function returns."""
    df = df.copy()

    # Common alternate names.
    rename = {}
    for old, new in {
        "timestamp": "time",
        "datetime": "time",
        "Date": "time",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    }.items():
        if old in df.columns and new not in df.columns:
            rename[old] = new

    if rename:
        df = df.rename(columns=rename)

    required = ["time", "open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"Production fetch returned missing columns: {missing}. "
            f"Columns: {list(df.columns)}"
        )

    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Handle either epoch seconds or an already-parsed datetime column.
    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        numeric_time = pd.to_numeric(df["time"], errors="coerce")
        if numeric_time.notna().all():
            unit = "ms" if numeric_time.abs().median() > 10**11 else "s"
            df["time"] = pd.to_datetime(numeric_time, unit=unit)
        else:
            df["time"] = pd.to_datetime(df["time"])

    return df.dropna(subset=["time", "open", "high", "low", "close"]).sort_values(
        "time"
    ).reset_index(drop=True)


def fetch_candles(engine):
    """
    Reuse the production engine's fetch function instead of hard-coding
    the old MEXC endpoint.

    We discover likely fetch functions by name and call the first compatible
    zero/one-argument function. If the engine exposes no fetch helper, fail
    with a clear message rather than guessing an endpoint.
    """
    # The current production engine exposes get_candles(symbol, interval, limit).
    # Use that exact function/signature first.
    production_get_candles = getattr(engine, "get_candles", None)
    if callable(production_get_candles):
        df = production_get_candles(
            getattr(engine, "SYMBOL", "LTC_USDT"),
            getattr(engine, "TIMEFRAME", "Min60"),
            LIMIT,
        )
        df = normalize_dataframe(df)
        if len(df) >= 100:
            print("Using production fetch function: get_candles()")
            return df.tail(LIMIT).reset_index(drop=True)

    candidates = [
        "fetch_market_data",
        "fetch_mexc_data",
        "get_market_data",
        "download_market_data",
        "get_klines",
        "fetch_klines",
    ]

    for name in candidates:
        fn = getattr(engine, name, None)
        if not callable(fn):
            continue

        attempts = [
            lambda: fn(),
            lambda: fn(limit=LIMIT),
            lambda: fn(LIMIT),
        ]

        for attempt in attempts:
            try:
                result = attempt()

                if isinstance(result, tuple):
                    # Prefer the first DataFrame-like item.
                    for item in result:
                        if isinstance(item, pd.DataFrame):
                            result = item
                            break

                if isinstance(result, pd.DataFrame):
                    df = normalize_dataframe(result)
                    if len(df) >= 100:
                        print(f"Using production fetch function: {name}()")
                        return df.tail(LIMIT).reset_index(drop=True)

            except TypeError:
                continue
            except Exception as exc:
                print(f"Production fetch function {name} failed: {exc}")
                break

    raise RuntimeError(
        "Could not find a compatible production candle-fetch function in "
        f"{ENGINE_FILE}. No fallback endpoint is used."
    )


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


def main():
    engine = load_engine_safely()
    df = fetch_candles(engine)

    print(f"Downloaded {len(df)} production-source candles")
    print("Discord/live runner was NOT executed.")

    if len(df) <= WARMUP:
        raise RuntimeError(
            f"Not enough candles for replay: {len(df)} <= warmup {WARMUP}"
        )

    snapshots = []
    cases = {}
    errors = 0

    # V3.4: case-anchored lifecycle replay.
    # Once a Step 6 case is discovered, retain its exact sweep + displacement
    # objects and re-evaluate Step 7 against every later closed snapshot.
    # This avoids depending on the CURRENT Step 6/Step 5 state after the case
    # has moved on.
    for end in range(WARMUP, len(df)):
        snap = df.iloc[:end].copy().reset_index(drop=True)
        asof = snap.iloc[-1]["time"]

        try:
            step5, step6, _live_step7 = production_pipeline(engine, snap)
        except Exception as exc:
            errors += 1
            snapshots.append({
                "asof": str(asof),
                "error": str(exc),
            })
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
                        "last_step7_status": None,
                        # Internal-only replay anchors.
                        "_sweep": sweep.copy() if hasattr(sweep, "copy") else dict(sweep),
                        "_displacement": displacement.copy() if hasattr(displacement, "copy") else dict(displacement),
                    }

        # Full lifecycle tracking: every known Step 6 case is evaluated
        # against the same anchored sweep/displacement on this snapshot.
        for case in cases.values():
            if case["step7_confirmed"]:
                continue

            try:
                sb = engine.detect_post_displacement_structure_break_v2(
                    snap,
                    case["_sweep"],
                    case["_displacement"],
                )
            except Exception as exc:
                case["last_step7_status"] = f"ERROR: {exc}"
                continue

            case["last_step7_status"] = sb.get("status")

            if sb.get("confirmed"):
                case["step7_confirmed"] = True
                case["step7_time"] = str(sb.get("break_time"))
                case["broken_level"] = sb.get("broken_level")

                # Step 8 is evaluated only after this case's own Step 7 BOS.
                anchored_step7 = {
                    "status": sb.get("status"),
                    "confirmed": True,
                    "structure_break": sb,
                }

                try:
                    step8 = engine.get_step8_status(snap, anchored_step7)
                except Exception as exc:
                    step8 = {
                        "confirmed": False,
                        "status": f"ERROR: {exc}",
                    }

                if step8.get("confirmed"):
                    retest = step8.get("retest") or {}
                    case["step8_confirmed"] = True
                    case["step8_time"] = str(retest.get("time"))

        snapshots.append({
            "asof": str(asof),
            "step5_active": bool(step5.get("latest")) if step5 else False,
            "step6_confirmed": bool(
                displacement and displacement.get("confirmed")
            ),
            "live_step7_status": (
                _live_step7.get("status") if _live_step7 else None
            ),
            "live_step7_confirmed": bool(
                _live_step7.get("confirmed")
            ) if _live_step7 else False,
            "anchored_bos_cases": sum(
                1 for c in cases.values() if c["step7_confirmed"]
            ),
            "anchored_retest_cases": sum(
                1 for c in cases.values() if c["step8_confirmed"]
            ),
        })

    # Do not serialize internal replay objects into the CSV report.
    public_cases = []
    for case in cases.values():
        public_cases.append({
            k: v for k, v in case.items() if not k.startswith("_")
        })

    cases_df = pd.DataFrame(public_cases)
    snapshots_df = pd.DataFrame(snapshots)

    cases_df.to_csv(
        "step7_v3_4_lifecycle_cases.csv",
        index=False,
    )
    snapshots_df.to_csv(
        "step7_v3_4_lifecycle_snapshots.csv",
        index=False,
    )

    bos_count = (
        int(cases_df["step7_confirmed"].sum())
        if not cases_df.empty else 0
    )
    retest_count = (
        int(cases_df["step8_confirmed"].sum())
        if not cases_df.empty else 0
    )

    print("\n=== STEP 7 V3.4 CASE-ANCHORED LIFECYCLE REPLAY ===")
    print(f"Candles loaded: {len(df)}")
    print(f"Snapshots processed: {len(snapshots)}")
    print(f"Engine errors: {errors}")
    print(f"Unique Step 6 cases: {len(cases_df)}")
    print(f"Cases with Step 7 V2 BOS: {bos_count}")
    print(f"Cases with Step 8 retest: {retest_count}")

    if not cases_df.empty:
        for _, row in cases_df.iterrows():
            print(
                f"Case {int(row['case_id'])}: "
                f"Step7={'BOS' if row['step7_confirmed'] else 'WAIT'} | "
                f"Step8={'RETEST' if row['step8_confirmed'] else 'WAIT'} | "
                f"Disp={row['displacement_time']}"
            )

    print("\nReports:")
    print(" - step7_v3_4_lifecycle_cases.csv")
    print(" - step7_v3_4_lifecycle_snapshots.csv")


if __name__ == "__main__":
    main()
