"""
Step 7 V3.1 Safe Replay Validator
Loads the production engine without executing its bottom-level live/Discord runner.
Tracks each Step 6 case through later closed candles.
"""

import ast
import requests
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
SYMBOL = "LTC_USDT"
INTERVAL = "Min60"
LIMIT = 1000
WARMUP = 300


def load_engine_safely():
    """Load function/class/constant definitions only; skip top-level runner."""
    source = Path(ENGINE_FILE).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=ENGINE_FILE)

    allowed = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            allowed.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            allowed.append(node)
        elif isinstance(node, ast.If):
            # Never execute module-level `if __name__ == "__main__"` runner.
            test = node.test
            if isinstance(test, ast.Compare):
                continue
        else:
            # Skip executable top-level expressions/calls.
            continue

    module = ast.Module(body=allowed, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {"__name__": "smc_engine_replay"}
    exec(compile(module, ENGINE_FILE, "exec"), namespace)

    class Engine:
        pass

    engine = Engine()
    for name, value in namespace.items():
        if not name.startswith("__"):
            setattr(engine, name, value)
    return engine


def fetch_candles():
    url = "https://api.mexc.com/api/v1/contract/kline"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    raw = r.json()["data"]

    df = pd.DataFrame(
        raw,
        columns=["time", "open", "high", "low", "close", "volume"]
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.sort_values("time").reset_index(drop=True)


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
    df = fetch_candles()

    print(f"Downloaded {len(df)} MEXC Futures candles")
    print("Discord/live runner was NOT executed.")

    snapshots = []
    cases = {}
    errors = 0

    for end in range(WARMUP, len(df)):
        snap = df.iloc[:end].copy().reset_index(drop=True)
        asof = snap.iloc[-1]["time"]

        try:
            step5, step6, step7 = production_pipeline(engine, snap)
        except Exception as e:
            errors += 1
            snapshots.append({
                "asof": str(asof),
                "error": str(e),
            })
            continue

        displacement = (
            step6.get("displacement")
            if step6 else None
        )

        if displacement and displacement.get("confirmed"):
            candle = displacement.get("candle") or {}
            sweep = step5.get("latest") if step5 else None

            key = (
                str(sweep.get("time") if sweep else None),
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
                }

        # Track every known case through later replay snapshots.
        # We intentionally do not generate a signal from future candles.
        for case in cases.values():
            current_disp_time = (
                str(displacement.get("candle", {}).get("time"))
                if displacement else None
            )

            if current_disp_time != case["displacement_time"]:
                continue

            case["last_step7_status"] = (
                step7.get("status") if step7 else None
            )

            if step7 and step7.get("confirmed") and not case["step7_confirmed"]:
                sb = step7.get("structure_break") or {}

                case["step7_confirmed"] = True
                case["step7_time"] = str(sb.get("break_time"))
                case["broken_level"] = sb.get("broken_level")

                try:
                    step8 = engine.get_step8_status(
                        snap,
                        step7,
                    )
                except Exception as e:
                    step8 = {
                        "confirmed": False,
                        "status": f"ERROR: {e}",
                    }

                if step8.get("confirmed"):
                    retest = step8.get("retest") or {}
                    case["step8_confirmed"] = True
                    case["step8_time"] = str(
                        retest.get("time")
                    )

        snapshots.append({
            "asof": str(asof),
            "step5_active": bool(
                step5.get("latest")
            ) if step5 else False,
            "step6_confirmed": bool(
                displacement and displacement.get("confirmed")
            ),
            "step7_status": (
                step7.get("status") if step7 else None
            ),
            "step7_confirmed": bool(
                step7.get("confirmed")
            ) if step7 else False,
            "step7_break_time": (
                (step7.get("structure_break") or {}).get("break_time")
                if step7 else None
            ),
        })

    cases_df = pd.DataFrame(list(cases.values()))
    snapshots_df = pd.DataFrame(snapshots)

    cases_df.to_csv(
        "step7_v3_1_lifecycle_cases.csv",
        index=False,
    )
    snapshots_df.to_csv(
        "step7_v3_1_lifecycle_snapshots.csv",
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

    print("\n=== STEP 7 V3.1 SAFE LIFECYCLE REPLAY ===")
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
    print(" - step7_v3_1_lifecycle_cases.csv")
    print(" - step7_v3_1_lifecycle_snapshots.csv")


if __name__ == "__main__":
    main()
