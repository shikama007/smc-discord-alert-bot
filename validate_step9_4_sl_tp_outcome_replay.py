import time
import requests
import pandas as pd

SYMBOL = "LTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
INTERVAL = "Min5"

CASES = [
    {"case_id": "case1", "direction": "SHORT", "entry_time": "2026-09-08 20:45:00", "entry": 54.00, "retest_wick_sl": 54.05, "structure_sl": 54.03},
    {"case_id": "case2", "direction": "SHORT", "entry_time": "2026-09-20 14:15:00", "entry": 56.80, "retest_wick_sl": 57.01, "structure_sl": 56.83},
    {"case_id": "case5", "direction": "LONG", "entry_time": "2026-09-26 08:10:00", "entry": 73.16, "retest_wick_sl": 73.06, "structure_sl": 73.07},
    {"case_id": "case6", "direction": "LONG", "entry_time": "2026-09-26 09:40:00", "entry": 74.07, "retest_wick_sl": 73.81, "structure_sl": 73.86},
]

REPLAY_CANDLES = 288

def fetch_5m_candles(start_time, end_time):
    all_rows = []
    cursor = start_time

    while cursor < end_time:
        response = requests.get(
            f"{BASE_URL}/{SYMBOL}",
            params={"interval": INTERVAL, "start": cursor, "end": end_time},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()

        if not result.get("success"):
            raise RuntimeError(f"MEXC API Error: {result}")

        data = result.get("data")
        if not data:
            break

        df = pd.DataFrame({
            "time": data["time"],
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data["vol"],
        })
        all_rows.append(df)

        last_time = int(df["time"].iloc[-1])
        if last_time <= cursor:
            break

        cursor = last_time + 300
        time.sleep(0.15)

    if not all_rows:
        return pd.DataFrame(columns=["time","open","high","low","close","volume"])

    df = pd.concat(all_rows, ignore_index=True).drop_duplicates("time")
    df = df.sort_values("time").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df

def parse_time(value):
    return pd.Timestamp(value, tz="UTC")

def calculate_risk(direction, entry, sl):
    return entry - sl if direction == "LONG" else sl - entry

def calculate_target(direction, entry, risk, multiple):
    return entry + risk * multiple if direction == "LONG" else entry - risk * multiple

def replay_trade(candles, direction, entry, sl, entry_time):
    risk = calculate_risk(direction, entry, sl)

    if risk <= 0:
        return {"result": "INVALID_SL", "exit_time": None, "exit_price": None,
                "r_multiple": None, "mae": None, "mfe": None, "risk": risk}

    tp2 = calculate_target(direction, entry, risk, 2)
    tp3 = calculate_target(direction, entry, risk, 3)

    future = candles[candles["time"] > entry_time].copy()
    if future.empty:
        return {"result": "NO_DATA", "exit_time": None, "exit_price": None,
                "r_multiple": None, "mae": None, "mfe": None,
                "risk": risk, "tp2": tp2, "tp3": tp3}

    mae = 0.0
    mfe = 0.0

    for _, candle in future.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        candle_time = candle["time"]

        if direction == "LONG":
            mae = max(mae, entry - low)
            mfe = max(mfe, high - entry)
            sl_hit = low <= sl
            tp2_hit = high >= tp2
            tp3_hit = high >= tp3
        else:
            mae = max(mae, high - entry)
            mfe = max(mfe, entry - low)
            sl_hit = high >= sl
            tp2_hit = low <= tp2
            tp3_hit = low <= tp3

        # Same-candle ambiguity: SL is counted first.
        if sl_hit:
            return {"result": "SL", "exit_time": candle_time, "exit_price": sl,
                    "r_multiple": -1.0, "mae": mae, "mfe": mfe,
                    "risk": risk, "tp2": tp2, "tp3": tp3}

        if tp3_hit:
            return {"result": "TP3", "exit_time": candle_time, "exit_price": tp3,
                    "r_multiple": 3.0, "mae": mae, "mfe": mfe,
                    "risk": risk, "tp2": tp2, "tp3": tp3}

        if tp2_hit:
            return {"result": "TP2", "exit_time": candle_time, "exit_price": tp2,
                    "r_multiple": 2.0, "mae": mae, "mfe": mfe,
                    "risk": risk, "tp2": tp2, "tp3": tp3}

    return {"result": "NO_OUTCOME", "exit_time": None, "exit_price": None,
            "r_multiple": None, "mae": mae, "mfe": mfe,
            "risk": risk, "tp2": tp2, "tp3": tp3}

def main():
    print("=" * 70)
    print("STEP 9.4 — SL / TP OUTCOME REPLAY")
    print("=" * 70)

    all_results = []

    for case in CASES:
        print(f"\n{case['case_id']} | {case['direction']} | {case['entry_time']}")

        entry_time = parse_time(case["entry_time"])
        start_time = int(entry_time.timestamp())
        end_time = start_time + REPLAY_CANDLES * 300

        candles = fetch_5m_candles(start_time, end_time)
        print(f"Candles fetched: {len(candles)}")

        if candles.empty:
            continue

        for model, sl in [
            ("RETEST_WICK", case["retest_wick_sl"]),
            ("5M_STRUCTURE", case["structure_sl"]),
        ]:
            result = replay_trade(
                candles, case["direction"], case["entry"], sl, entry_time
            )

            all_results.append({
                "case_id": case["case_id"],
                "direction": case["direction"],
                "entry_time": entry_time,
                "entry": case["entry"],
                "sl_model": model,
                "sl": sl,
                **result,
            })

            print(
                f"  {model}: SL={sl:.4f} | "
                f"Result={result['result']} | "
                f"R={result.get('r_multiple')} | "
                f"MAE={result.get('mae')} | "
                f"MFE={result.get('mfe')}"
            )

    results_df = pd.DataFrame(all_results)
    results_df.to_csv("step9_4_sl_tp_outcome_cases.csv", index=False)

    summary_rows = []
    for model in ["RETEST_WICK", "5M_STRUCTURE"]:
        subset = results_df[results_df["sl_model"] == model]
        if subset.empty:
            continue

        r = subset["r_multiple"].dropna()
        summary_rows.append({
            "sl_model": model,
            "total_cases": len(subset),
            "tp2": int((subset["result"] == "TP2").sum()),
            "tp3": int((subset["result"] == "TP3").sum()),
            "sl": int((subset["result"] == "SL").sum()),
            "no_outcome": int((subset["result"] == "NO_OUTCOME").sum()),
            "total_r": r.sum(),
            "avg_r": r.mean() if not r.empty else None,
        })

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv("step9_4_sl_tp_outcome_summary.csv", index=False)

    print("\n" + "=" * 70)
    print("STEP 9.4 SUMMARY")
    print("=" * 70)
    if not summary_df.empty:
        print(summary_df.to_string(index=False))

    print("\nCreated:")
    print("  step9_4_sl_tp_outcome_cases.csv")
    print("  step9_4_sl_tp_outcome_summary.csv")

if __name__ == "__main__":
    main()
