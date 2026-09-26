"""
SMC / ICT Step 6 -> Step 7 Diagnostic Replay V5

Purpose:
- Diagnostic only. Does NOT modify production Step 7.
- Trace what happens after each unique Step 6 displacement:
    Sweep -> Displacement -> post-displacement price action
    -> internal structure candidates -> confirmed swings -> BOS
- Compare candidate definitions and report why the current Step 7 may stop.
- Uses only candles that were closed at each replay snapshot.
"""

import importlib.util
import json
import sys
from pathlib import Path

ENGINE_CANDIDATES = [
    "smc_engine_step8_full.py",
    "smc_engine_step6_step5_same_candle_poi_fix.py",
]
ENGINE_PATH = next((Path(x) for x in ENGINE_CANDIDATES if Path(x).exists()), None)
if ENGINE_PATH is None:
    raise FileNotFoundError("Production engine file not found.")

# The production engine has an unguarded MAIN section that sends Discord alerts
# when the module is imported. V5 is diagnostic-only, so load only the engine
# definitions/constants and stop immediately before its MAIN section.
source = ENGINE_PATH.read_text(encoding="utf-8")
main_marker = "# =========================================================\n# MAIN\n# ========================================================="
if main_marker not in source:
    raise RuntimeError("Could not find the production engine MAIN marker.")
source = source.split(main_marker, 1)[0]
engine_namespace = {"__name__": "engine_v5_loaded"}
exec(compile(source, str(ENGINE_PATH), "exec"), engine_namespace)

class EngineNamespace:
    pass
engine = EngineNamespace()
for _name, _value in engine_namespace.items():
    if not _name.startswith("__"):
        setattr(engine, _name, _value)

import pandas as pd
import requests

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "LTC_USDT"
TIMEFRAME = "Min60"
FETCH_LIMIT = 1000
WARMUP = 200
MAX_SNAPSHOTS = 700

POST_DISPLACEMENT_WINDOW = 40
SWING_STRENGTH = 2
BREAK_WINDOW = 40

def fetch():
    r = requests.get(BASE_URL, params={
        "symbol": SYMBOL,
        "interval": TIMEFRAME,
        "start": 0,
        "limit": FETCH_LIMIT
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    rows = data.get("data", data)
    if not rows:
        raise RuntimeError("No kline data returned.")
    # MEXC contract kline commonly returns [time, open, high, low, close, vol, ...]
    out = []
    for row in rows:
        out.append({
            "time": pd.to_datetime(int(row[0]), unit="s"),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    return pd.DataFrame(out).drop_duplicates("time").sort_values("time").reset_index(drop=True)

def safe_json(x):
    try:
        return json.dumps(x, default=str, ensure_ascii=False)
    except Exception:
        return str(x)

def swing_candidates_after_displacement(closed, displacement_index, direction, window=POST_DISPLACEMENT_WINDOW):
    """
    Diagnostic candidate scan.

    We inspect confirmed pivots AND raw local structure candidates.
    A candidate is never treated as a production signal here.
    """
    end = min(len(closed), displacement_index + 1 + window)
    start = displacement_index + 1
    rows = []

    # Confirmed pivot candidates: require strength bars on both sides.
    for i in range(max(start, SWING_STRENGTH), end - SWING_STRENGTH):
        h = float(closed.loc[i, "high"])
        l = float(closed.loc[i, "low"])
        lh = closed.loc[i-SWING_STRENGTH:i-1, "high"]
        rh = closed.loc[i+1:i+SWING_STRENGTH, "high"]
        ll = closed.loc[i-SWING_STRENGTH:i-1, "low"]
        rl = closed.loc[i+1:i+SWING_STRENGTH, "low"]

        if direction == "BULLISH" and h > lh.max() and h > rh.max():
            rows.append({
                "candidate_kind": "CONFIRMED_SWING_HIGH",
                "index": i,
                "time": closed.loc[i, "time"],
                "price": h,
            })
        if direction == "BEARISH" and l < ll.min() and l < rl.min():
            rows.append({
                "candidate_kind": "CONFIRMED_SWING_LOW",
                "index": i,
                "time": closed.loc[i, "time"],
                "price": l,
            })

    # Raw candidate: extremum before the next two candles confirm it.
    # This is diagnostic only and explicitly not used as a BOS level until confirmed.
    for i in range(start, end):
        if i + SWING_STRENGTH >= len(closed):
            break
        h = float(closed.loc[i, "high"])
        l = float(closed.loc[i, "low"])
        future_highs = closed.loc[i+1:i+SWING_STRENGTH, "high"]
        future_lows = closed.loc[i+1:i+SWING_STRENGTH, "low"]

        if direction == "BULLISH":
            if h >= float(closed.loc[max(start, i):i+1, "high"].max()) and h >= float(future_highs.max()):
                rows.append({
                    "candidate_kind": "RAW_HIGH_CANDIDATE",
                    "index": i,
                    "time": closed.loc[i, "time"],
                    "price": h,
                })
        else:
            if l <= float(closed.loc[max(start, i):i+1, "low"].min()) and l <= float(future_lows.min()):
                rows.append({
                    "candidate_kind": "RAW_LOW_CANDIDATE",
                    "index": i,
                    "time": closed.loc[i, "time"],
                    "price": l,
                })
    return rows

def later_bos(closed, candidate, direction, from_index, window=BREAK_WINDOW):
    if not candidate:
        return None
    level = float(candidate["price"])
    end = min(len(closed), from_index + 1 + window)
    for i in range(from_index + 1, end):
        c = float(closed.loc[i, "close"])
        if direction == "BULLISH" and c > level:
            return {
                "index": i, "time": closed.loc[i, "time"], "close": c,
                "level": level, "direction": direction
            }
        if direction == "BEARISH" and c < level:
            return {
                "index": i, "time": closed.loc[i, "time"], "close": c,
                "level": level, "direction": direction
            }
    return None

def retest_after_bos(closed, bos, direction, window=BREAK_WINDOW):
    if not bos:
        return None
    level = float(bos["level"])
    start = int(bos["index"]) + 1
    end = min(len(closed), start + window)
    for i in range(start, end):
        h = float(closed.loc[i, "high"])
        l = float(closed.loc[i, "low"])
        c = float(closed.loc[i, "close"])
        touched = l <= level <= h
        valid = touched and ((direction == "BULLISH" and c > level) or
                             (direction == "BEARISH" and c < level))
        if valid:
            return {
                "index": i, "time": closed.loc[i, "time"],
                "close": c, "level": level, "direction": direction
            }
    return None

def main():
    df = fetch()
    snapshots = []
    start = max(WARMUP, 1)
    end = min(len(df) - 1, start + MAX_SNAPSHOTS)

    for n in range(start, end):
        # At snapshot n, last row is the currently forming candle; production
        # engine excludes it. We replay through n only.
        snap = df.iloc[:n+1].copy().reset_index(drop=True)
        try:
            step5 = engine.get_step5_status(snap)
            step6 = engine.get_step6_status(snap, step5)
        except Exception as e:
            continue

        disp = step6.get("displacement") if isinstance(step6, dict) else None
        if not disp or not disp.get("confirmed"):
            continue

        dtime = str(disp.get("candle", {}).get("time"))
        if not dtime:
            continue

        snapshots.append({
            "asof": str(snap.iloc[-1]["time"]),
            "displacement_time": dtime,
            "direction": disp.get("direction"),
            "sweep_time": str((step5.get("latest") or {}).get("time")),
            "step6_status": step6.get("status"),
        })

    unique = []
    seen = set()
    for x in snapshots:
        key = (x["displacement_time"], x["direction"])
        if key not in seen:
            seen.add(key)
            unique.append(x)

    cases = []
    candidates = []

    for case_id, u in enumerate(unique, 1):
        dtime = pd.to_datetime(u["displacement_time"])
        matches = df.index[df["time"] == dtime].tolist()
        if not matches:
            continue
        di = int(matches[-1])
        # Need candles after displacement; use full historical data here only
        # for diagnostic reconstruction. No production signal is emitted.
        closed = df.iloc[:di+1].copy().reset_index(drop=True)
        direction = u["direction"]

        # Current production strict candidates before displacement
        strict = []
        if hasattr(engine, "_find_post_sweep_internal_levels"):
            try:
                sweep_time = pd.to_datetime(u["sweep_time"])
                sm = closed.index[closed["time"] == sweep_time].tolist()
                si = int(sm[-1]) if sm else -1
                if si >= 0:
                    strict = engine._find_post_sweep_internal_levels(
                        closed, si, di, direction, strength=2
                    )
            except Exception:
                strict = []

        # Extended diagnostic structure scan after displacement
        post = swing_candidates_after_displacement(
            df.iloc[:min(len(df)-1, di+1+POST_DISPLACEMENT_WINDOW)].copy().reset_index(drop=True),
            di, direction, POST_DISPLACEMENT_WINDOW
        )

        # Prefer confirmed candidates; evaluate later BOS and retest.
        confirmed = [x for x in post if x["candidate_kind"] == "CONFIRMED_SWING_HIGH" or
                     x["candidate_kind"] == "CONFIRMED_SWING_LOW"]

        bos_found = None
        bos_candidate = None
        retest_found = None

        for c in confirmed:
            b = later_bos(
                df.iloc[:min(len(df)-1, di+1+POST_DISPLACEMENT_WINDOW+BREAK_WINDOW)]
                  .copy().reset_index(drop=True),
                c, direction, int(c["index"]), BREAK_WINDOW
            )
            if b:
                bos_found = b
                bos_candidate = c
                retest_found = retest_after_bos(
                    df.iloc[:min(len(df)-1, b["index"]+1+BREAK_WINDOW)]
                      .copy().reset_index(drop=True),
                    b, direction, BREAK_WINDOW
                )
                break

        cases.append({
            "case_id": case_id,
            "displacement_time": dtime,
            "direction": direction,
            "sweep_time": u["sweep_time"],
            "strict_internal_levels_before_displacement": len(strict),
            "post_displacement_candidates": len(post),
            "confirmed_post_displacement_swings": len(confirmed),
            "diagnostic_bos_found": bool(bos_found),
            "diagnostic_bos_time": bos_found["time"] if bos_found else "",
            "diagnostic_bos_level": bos_found["level"] if bos_found else "",
            "diagnostic_retest_found": bool(retest_found),
            "diagnostic_retest_time": retest_found["time"] if retest_found else "",
        })

        for c in post:
            b = later_bos(
                df.iloc[:min(len(df)-1, c["index"]+1+BREAK_WINDOW)].copy().reset_index(drop=True),
                c, direction, int(c["index"]), BREAK_WINDOW
            )
            candidates.append({
                "case_id": case_id,
                "displacement_time": dtime,
                "direction": direction,
                "sweep_time": u["sweep_time"],
                **c,
                "diagnostic_bos_found": bool(b),
                "diagnostic_bos_time": b["time"] if b else "",
                "diagnostic_bos_level": b["level"] if b else "",
            })

    pd.DataFrame(cases).to_csv("step5_step7_replay_v5_cases.csv", index=False)
    pd.DataFrame(candidates).to_csv("step5_step7_replay_v5_candidates.csv", index=False)

    print("=== V5 DIAGNOSTIC SUMMARY ===")
    print("Unique Step 6 cases:", len(unique))
    print("Cases with strict internal level:", sum(x["strict_internal_levels_before_displacement"] > 0 for x in cases))
    print("Cases with post-displacement candidates:", sum(x["post_displacement_candidates"] > 0 for x in cases))
    print("Cases with confirmed post-displacement swings:", sum(x["confirmed_post_displacement_swings"] > 0 for x in cases))
    print("Cases with diagnostic BOS:", sum(bool(x["diagnostic_bos_found"]) for x in cases))
    print("Cases with diagnostic BOS + retest:", sum(bool(x["diagnostic_retest_found"]) for x in cases))
    print("Reports:")
    print(" - step5_step7_replay_v5_cases.csv")
    print(" - step5_step7_replay_v5_candidates.csv")

if __name__ == "__main__":
    main()
