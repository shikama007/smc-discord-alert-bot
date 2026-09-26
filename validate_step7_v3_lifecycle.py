"""
Step 7 V3 Replay Validator
Tracks each confirmed Step 6 case forward through ALL later closed candles.
Production Step 7 V2 engine is not modified.
"""

import importlib.util
import requests
import pandas as pd

ENGINE_FILE = "smc_engine_step8_full.py"
SYMBOL = "LTC_USDT"
INTERVAL = "Min60"
LIMIT = 1000
WARMUP = 300

def load_engine():
    spec = importlib.util.spec_from_file_location("engine", ENGINE_FILE)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine

def fetch_candles():
    url = "https://api.mexc.com/api/v1/contract/kline"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    raw = r.json()["data"]
    df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.sort_values("time").reset_index(drop=True)

def production_pipeline(engine, snap):
    swing_strength = engine.SWING_STRENGTH
    swing_highs, swing_lows = engine.find_swings(snap, swing_strength)
    events = engine.detect_historical_events(snap, swing_highs, swing_lows)
    structure = engine.get_current_structure(swing_highs, swing_lows, events, df=snap)
    bias = structure["bias"]
    liquidity = engine.analyze_liquidity(snap, swing_highs, swing_lows)
    premium_discount = engine.analyze_premium_discount(snap, swing_highs, swing_lows, bias)
    fvgs = engine.detect_fvgs(snap)
    order_blocks = engine.detect_order_blocks(snap, events)
    supply_demand = engine.detect_supply_demand(snap, events)
    preferred_pois, _ = engine.select_relevant_pois(
        fvgs, order_blocks, supply_demand,
        liquidity["current_price"], bias,
        premium_discount=premium_discount, events=events, df=snap,
    )
    all_pois = (
        [{**x, "category": "FVG"} for x in fvgs]
        + [{**x, "category": "ORDER BLOCK"} for x in order_blocks]
        + [{**x, "category": "SUPPLY/DEMAND"} for x in supply_demand]
    )
    sweeps = engine.detect_liquidity_sweeps(
        snap, swing_highs, swing_lows,
        preferred_pois=preferred_pois, all_pois=all_pois,
    )
    step5 = engine.get_step5_status(
        sweeps, bias, preferred_pois=preferred_pois, events=events
    )
    step6 = engine.get_step6_status(snap, step5, bias)
    step7 = engine.get_step7_status(snap, step5, step6)
    return step5, step6, step7

def main():
    engine = load_engine()
    df = fetch_candles()
    print(f"Downloaded {len(df)} MEXC Futures candles")

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
            snapshots.append({"asof": str(asof), "error": str(e)})
            continue

        s6 = step6.get("displacement") if step6 else None

        if s6 and s6.get("confirmed") and s6.get("candle"):
            disp = s6["candle"]
            sweep = step5.get("latest") if step5 else None
            key = (str(sweep.get("time") if sweep else None),
                   str(disp.get("time")), s6.get("direction"))
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

        # IMPORTANT: evaluate the current snapshot against every known case.
        # This is the lifecycle tracking missing from the previous validator.
        for case in cases.values():
            current_s6_time = str(s6.get("candle", {}).get("time")) if s6 else None
            if current_s6_time != case["displacement_time"]:
                continue

            case["last_step7_status"] = step7.get("status") if step7 else None

            if step7 and step7.get("confirmed") and not case["step7_confirmed"]:
                sb = step7.get("structure_break") or {}
                case["step7_confirmed"] = True
                case["step7_time"] = str(sb.get("break_time"))
                case["broken_level"] = sb.get("broken_level")

                try:
                    step8 = engine.get_step8_status(snap, step7)
                except Exception as e:
                    step8 = {"confirmed": False, "status": f"ERROR: {e}"}
                if step8.get("confirmed"):
                    retest = step8.get("retest") or {}
                    case["step8_confirmed"] = True
                    case["step8_time"] = str(retest.get("time"))

        snapshots.append({
            "asof": str(asof),
            "step5_active": bool(step5.get("latest")) if step5 else False,
            "step6_confirmed": bool(s6 and s6.get("confirmed")),
            "step7_status": step7.get("status") if step7 else None,
            "step7_confirmed": bool(step7.get("confirmed")) if step7 else False,
            "step7_break_time": ((step7.get("structure_break") or {}).get("break_time")
                                 if step7 else None),
        })

    cases_df = pd.DataFrame(list(cases.values()))
    pd.DataFrame(snapshots).to_csv("step7_v3_lifecycle_snapshots.csv", index=False)
    cases_df.to_csv("step7_v3_lifecycle_cases.csv", index=False)

    print("\n=== STEP 7 V3 LIFECYCLE REPLAY ===")
    print(f"Candles loaded: {len(df)}")
    print(f"Snapshots processed: {len(snapshots)}")
    print(f"Engine errors: {errors}")
    print(f"Unique Step 6 cases: {len(cases_df)}")
    print(f"Cases with Step 7 V2 BOS: {int(cases_df['step7_confirmed'].sum()) if not cases_df.empty else 0}")
    print(f"Cases with Step 8 retest: {int(cases_df['step8_confirmed'].sum()) if not cases_df.empty else 0}")

    for _, row in cases_df.iterrows():
        print(
            f"Case {int(row['case_id'])}: "
            f"Step7={'BOS' if row['step7_confirmed'] else 'WAIT'} | "
            f"Step8={'RETEST' if row['step8_confirmed'] else 'WAIT'} | "
            f"Disp={row['displacement_time']}"
        )

    print("Reports:")
    print(" - step7_v3_lifecycle_cases.csv")
    print(" - step7_v3_lifecycle_snapshots.csv")

if __name__ == "__main__":
    main()
