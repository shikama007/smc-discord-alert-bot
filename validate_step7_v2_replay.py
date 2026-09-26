"""
SMC / ICT Step 7 V2 Replay Validation

Purpose:
- Validate the production Step 7 V2 + Step 8 logic with a historical replay.
- Uses the exact production pipeline through Step 5 -> Step 6 -> Step 7 V2 -> Step 8.
- Only closed candles are allowed to confirm Step 7 / Step 8.
- No diagnostic candidate is treated as a production signal.

Outputs:
- step7_v2_replay_cases.csv
- step7_v2_replay_snapshots.csv
"""

import json
from pathlib import Path
import pandas as pd
import requests

ENGINE_CANDIDATES = [
    "smc_engine_step8_full.py",
    "smc_engine_step6_step5_same_candle_poi_fix.py",
]
ENGINE_PATH = next((Path(x) for x in ENGINE_CANDIDATES if Path(x).exists()), None)
if ENGINE_PATH is None:
    raise FileNotFoundError("Production engine file not found.")

source = ENGINE_PATH.read_text(encoding="utf-8")
main_marker = "# =========================================================\n# MAIN\n# ========================================================="
if main_marker not in source:
    raise RuntimeError("Could not find the production engine MAIN marker.")
source = source.split(main_marker, 1)[0]
engine_namespace = {"__name__": "engine_step7_v2_replay"}
exec(compile(source, str(ENGINE_PATH), "exec"), engine_namespace)

class EngineNamespace:
    pass

engine = EngineNamespace()
for name, value in engine_namespace.items():
    if not name.startswith("__"):
        setattr(engine, name, value)

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "LTC_USDT"
TIMEFRAME = "Min60"
FETCH_LIMIT = 1000
WARMUP = 200
MAX_SNAPSHOTS = 700


def fetch():
    url = f"{BASE_URL}/{SYMBOL}"
    r = requests.get(url, params={"interval": TIMEFRAME}, timeout=30)
    r.raise_for_status()
    result = r.json()
    if not result.get("success"):
        raise RuntimeError(f"MEXC API Error: {result}")

    data = result["data"]
    df = pd.DataFrame({
        "time": data["time"],
        "open": data["open"],
        "high": data["high"],
        "low": data["low"],
        "close": data["close"],
        "volume": data["vol"],
    })
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = (
        df.dropna()
          .drop_duplicates("time")
          .sort_values("time")
          .tail(FETCH_LIMIT)
          .reset_index(drop=True)
    )

    if len(df) < WARMUP + 50:
        raise RuntimeError(f"Not enough candles returned: {len(df)}")

    print(f"📡 Downloaded {len(df)} MEXC Futures candles")
    return df


def safe_json(value):
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return str(value)


def production_pipeline(snap):
    swing_strength = engine.SWING_STRENGTH
    swing_highs, swing_lows = engine.find_swings(snap, swing_strength)
    events = engine.detect_historical_events(snap, swing_highs, swing_lows)
    structure = engine.get_current_structure(
        swing_highs, swing_lows, events, df=snap
    )
    liquidity = engine.analyze_liquidity(snap, swing_highs, swing_lows)
    premium_discount = engine.analyze_premium_discount(
        snap, swing_highs, swing_lows, structure["bias"]
    )
    fvgs = engine.detect_fvgs(snap)
    order_blocks = engine.detect_order_blocks(snap, events)
    supply_demand = engine.detect_supply_demand(snap, events)

    preferred_pois, _ = engine.select_relevant_pois(
        fvgs,
        order_blocks,
        supply_demand,
        liquidity["current_price"],
        structure["bias"],
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
        structure["bias"],
        preferred_pois=preferred_pois,
        events=events,
    )
    step6 = engine.get_step6_status(
        snap,
        step5,
        structure["bias"],
    )
    step7 = engine.get_step7_status(snap, step5, step6)
    step8 = engine.get_step8_status(snap, step7)

    return structure, step5, step6, step7, step8


def get_disp_key(step6):
    disp = step6.get("displacement") if isinstance(step6, dict) else None
    if not disp or not disp.get("confirmed"):
        return None
    candle = disp.get("candle") or {}
    time = candle.get("time")
    direction = disp.get("direction")
    if time is None or direction not in ("BULLISH", "BEARISH"):
        return None
    return (str(time), direction)


def main():
    df = fetch()

    snapshots_processed = 0
    engine_errors = 0
    step5_active_count = 0
    step6_confirmed_count = 0
    step7_confirmed_count = 0
    step8_confirmed_count = 0
    first_step6 = None
    first_step7 = None
    first_step8 = None

    # Per unique Step 6 case. Each case is updated only with events observed
    # at the actual replay snapshot where the production pipeline reported them.
    cases = {}
    snapshot_rows = []

    start = max(WARMUP, 1)
    end = min(len(df) - 1, start + MAX_SNAPSHOTS)

    for n in range(start, end):
        snap = df.iloc[:n + 1].copy().reset_index(drop=True)
        asof = str(snap.iloc[-1]["time"])
        snapshots_processed += 1

        try:
            structure, step5, step6, step7, step8 = production_pipeline(snap)
        except Exception as exc:
            engine_errors += 1
            print(f"[ERROR] snapshot {asof}: {exc}")
            continue

        sweep = step5.get("latest") if isinstance(step5, dict) else None
        disp = step6.get("displacement") if isinstance(step6, dict) else None
        sb = step7.get("structure_break") if isinstance(step7, dict) else None
        rt = step8.get("retest") if isinstance(step8, dict) else None

        if sweep:
            step5_active_count += 1
        if disp and disp.get("confirmed"):
            step6_confirmed_count += 1
        if step7.get("confirmed"):
            step7_confirmed_count += 1
        if step8.get("confirmed"):
            step8_confirmed_count += 1

        key = get_disp_key(step6)
        if key:
            if key not in cases:
                cases[key] = {
                    "case_id": len(cases) + 1,
                    "displacement_time": key[0],
                    "direction": key[1],
                    "first_seen_asof": asof,
                    "sweep_time": str((sweep or {}).get("time", "")),
                    "step6_status": str(step6.get("status", "")),
                    "step7_confirmed": False,
                    "step7_status_at_confirmation": "",
                    "swing_time": "",
                    "swing_confirmation_time": "",
                    "broken_level": "",
                    "bos_time": "",
                    "bos_close": "",
                    "step7_reason": "",
                    "step8_confirmed": False,
                    "step8_status_at_confirmation": "",
                    "retest_time": "",
                    "retest_close": "",
                    "retest_level": "",
                }

            case = cases[key]

            if step7.get("confirmed") and not case["step7_confirmed"]:
                case["step7_confirmed"] = True
                case["step7_status_at_confirmation"] = str(step7.get("status", ""))
                case["swing_time"] = str((sb or {}).get("swing_time", ""))
                case["swing_confirmation_time"] = str((sb or {}).get("swing_confirmation_time", ""))
                case["broken_level"] = (sb or {}).get("broken_level", "")
                case["bos_time"] = str((sb or {}).get("break_time", ""))
                case["bos_close"] = (sb or {}).get("candle", {}).get("close", "") if sb else ""
                case["step7_reason"] = str((sb or {}).get("reason", ""))

                if first_step7 is None:
                    first_step7 = {
                        "asof": asof,
                        "case": case["case_id"],
                        "displacement_time": key[0],
                        "step7": sb,
                    }
                    print("\n=== FIRST STEP 7 V2 CONFIRMATION ===")
                    print(safe_json(first_step7))
                    print("====================================\n")

            if step8.get("confirmed") and not case["step8_confirmed"]:
                case["step8_confirmed"] = True
                case["step8_status_at_confirmation"] = str(step8.get("status", ""))
                case["retest_time"] = str((rt or {}).get("time", ""))
                case["retest_close"] = (rt or {}).get("close", "")
                case["retest_level"] = (rt or {}).get("level", "")

                if first_step8 is None:
                    first_step8 = {
                        "asof": asof,
                        "case": case["case_id"],
                        "displacement_time": key[0],
                        "step8": rt,
                    }
                    print("\n=== FIRST STEP 8 RETEST CONFIRMATION ===")
                    print(safe_json(first_step8))
                    print("========================================\n")

            snapshot_rows.append({
                "asof": asof,
                "displacement_time": key[0] if key else "",
                "direction": key[1] if key else "",
                "step5_active": bool(sweep),
                "step6_confirmed": bool(disp and disp.get("confirmed")),
                "step7_status": step7.get("status", ""),
                "step7_confirmed": bool(step7.get("confirmed")),
                "step7_swing_time": str((sb or {}).get("swing_time", "")),
                "step7_swing_confirmation_time": str((sb or {}).get("swing_confirmation_time", "")),
                "step7_bos_time": str((sb or {}).get("break_time", "")),
                "step7_broken_level": (sb or {}).get("broken_level", ""),
                "step8_status": step8.get("status", ""),
                "step8_confirmed": bool(step8.get("confirmed")),
                "step8_retest_time": str((rt or {}).get("time", "")),
                "step8_retest_level": (rt or {}).get("level", ""),
            })

    case_rows = list(cases.values())
    pd.DataFrame(case_rows).to_csv("step7_v2_replay_cases.csv", index=False)
    pd.DataFrame(snapshot_rows).to_csv("step7_v2_replay_snapshots.csv", index=False)

    print("\n=== STEP 7 V2 REPLAY SUMMARY ===")
    print("Candles loaded:", len(df))
    print("Snapshots processed:", snapshots_processed)
    print("Engine errors:", engine_errors)
    print("Snapshots with active Step 5 sweep:", step5_active_count)
    print("Snapshots with confirmed Step 6:", step6_confirmed_count)
    print("Unique Step 6 cases:", len(case_rows))
    print("Snapshots with confirmed Step 7 V2:", step7_confirmed_count)
    print("Snapshots with confirmed Step 8 retest:", step8_confirmed_count)
    print("Unique cases with Step 7 V2 BOS:", sum(bool(x["step7_confirmed"]) for x in case_rows))
    print("Unique cases with Step 8 retest:", sum(bool(x["step8_confirmed"]) for x in case_rows))

    if first_step6:
        print("First Step 6 confirmation captured: YES")
    else:
        print("First Step 6 confirmation captured: NO")
    if first_step7:
        print("First Step 7 V2 confirmation captured: YES")
    else:
        print("First Step 7 V2 confirmation captured: NO")
    if first_step8:
        print("First Step 8 retest confirmation captured: YES")
    else:
        print("First Step 8 retest confirmation captured: NO")

    print("\nCases:")
    for row in case_rows:
        print(
            f"Case {row['case_id']}: "
            f"Step7={'BOS' if row['step7_confirmed'] else 'WAIT'} | "
            f"Step8={'RETEST' if row['step8_confirmed'] else 'WAIT'} | "
            f"Disp={row['displacement_time']}"
        )

    print("\nReports:")
    print(" - step7_v2_replay_cases.csv")
    print(" - step7_v2_replay_snapshots.csv")
    print("===============================")


if __name__ == "__main__":
    main()
