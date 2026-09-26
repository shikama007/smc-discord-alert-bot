import time
import requests
import pandas as pd

# ============================================================
# STEP 9.4 — SL / TP OUTCOME REPLAY
# ============================================================
# Purpose:
#   Replay the four Step 9.2 qualified cases using exact 5M
#   MEXC futures candles and compare:
#
#     1. RETEST_WICK SL
#     2. 5M_STRUCTURE SL
#
# Targets:
#     TP2 = 2R
#     TP3 = 3R
#
# Rule:
#     If SL and TP are touched in the same candle,
#     SL is counted FIRST (conservative rule).
#
# Production engine is NOT modified by this validator.
# ============================================================

SYMBOL = "LTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
INTERVAL = "Min5"

# 24 hours of 5M candles after entry.
# 24 * 60 / 5 = 288 candles.
REPLAY_CANDLES = 288

REQUEST_TIMEOUT = 20
REQUEST_SLEEP = 0.20
MAX_EMPTY_RETRIES = 3


# ============================================================
# STEP 9.2 QUALIFIED CASES
# ============================================================

CASES = [
    {
        "case_id": "case1",
        "direction": "SHORT",
        "entry_time": "2026-09-08 20:45:00",
        "entry": 54.00,
        "retest_wick_sl": 54.05,
        "structure_sl": 54.03,
    },
    {
        "case_id": "case2",
        "direction": "SHORT",
        "entry_time": "2026-09-20 14:15:00",
        "entry": 56.80,
        "retest_wick_sl": 57.01,
        "structure_sl": 56.83,
    },
    {
        "case_id": "case5",
        "direction": "LONG",
        "entry_time": "2026-09-26 08:10:00",
        "entry": 73.16,
        "retest_wick_sl": 73.06,
        "structure_sl": 73.07,
    },
    {
        "case_id": "case6",
        "direction": "LONG",
        "entry_time": "2026-09-26 09:40:00",
        "entry": 74.07,
        "retest_wick_sl": 73.81,
        "structure_sl": 73.86,
    },
]


# ============================================================
# TIME
# ============================================================

def parse_time(value):
    return pd.Timestamp(value, tz="UTC")


# ============================================================
# MEXC FETCH
# ============================================================

def fetch_5m_candles(start_time, end_time):
    """
    Fetch 5M MEXC futures candles.

    Important fix:
    MEXC can occasionally return an empty data batch.
    The old version attempted:

        df["time"].iloc[-1]

    even when df was empty, causing:

        IndexError: single positional indexer is out-of-bounds

    This version safely handles empty batches and also prevents
    the pagination cursor from getting stuck in an infinite loop.
    """

    all_rows = []
    cursor = int(start_time)

    empty_retries = 0

    while cursor < int(end_time):

        print(
            f"    Fetching from "
            f"{pd.to_datetime(cursor, unit='s', utc=True)}"
        )

        try:
            response = requests.get(
                f"{BASE_URL}/{SYMBOL}",
                params={
                    "interval": INTERVAL,
                    "start": cursor,
                    "end": int(end_time),
                },
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

        except requests.RequestException as exc:

            print(
                f"    Request error: {exc}"
            )

            empty_retries += 1

            if empty_retries >= MAX_EMPTY_RETRIES:
                raise

            time.sleep(2)
            continue

        result = response.json()

        if not result.get("success"):

            raise RuntimeError(
                f"MEXC API Error: {result}"
            )

        data = result.get("data")

        # ----------------------------------------------------
        # FIX: empty API response
        # ----------------------------------------------------

        if not data:

            empty_retries += 1

            print(
                f"    Empty MEXC batch "
                f"(attempt {empty_retries}/{MAX_EMPTY_RETRIES})"
            )

            if empty_retries >= MAX_EMPTY_RETRIES:

                print(
                    "    Empty response limit reached. "
                    "Stopping this fetch."
                )

                break

            # Move forward one 5M candle and retry.
            cursor += 300

            time.sleep(1)

            continue

        # We received valid data.
        empty_retries = 0

        # ----------------------------------------------------
        # Build dataframe
        # ----------------------------------------------------

        df = pd.DataFrame({
            "time": data["time"],
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data["vol"],
        })

        # ----------------------------------------------------
        # FIX: dataframe safety
        # ----------------------------------------------------

        if df.empty:

            print(
                "    MEXC returned an empty dataframe."
            )

            cursor += 300

            time.sleep(1)

            continue

        # Make sure timestamp column is numeric.
        df["time"] = pd.to_numeric(
            df["time"],
            errors="coerce"
        )

        df = df.dropna(
            subset=["time"]
        )

        if df.empty:

            print(
                "    No valid timestamps in MEXC batch."
            )

            cursor += 300

            time.sleep(1)

            continue

        # ----------------------------------------------------
        # SAFE LAST TIMESTAMP
        # ----------------------------------------------------

        last_time = int(
            df["time"].iloc[-1]
        )

        # ----------------------------------------------------
        # PREVENT CURSOR STALL
        # ----------------------------------------------------

        if last_time <= cursor:

            print(
                f"    Cursor did not advance "
                f"(cursor={cursor}, last={last_time})."
            )

            cursor += 300

        else:

            cursor = last_time + 300

        all_rows.append(df)

        time.sleep(REQUEST_SLEEP)

    # ========================================================
    # NO DATA
    # ========================================================

    if not all_rows:

        return pd.DataFrame(
            columns=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

    # ========================================================
    # COMBINE
    # ========================================================

    df = pd.concat(
        all_rows,
        ignore_index=True
    )

    # ========================================================
    # CLEAN
    # ========================================================

    df = (
        df
        .drop_duplicates(
            subset=["time"]
        )
        .sort_values(
            "time"
        )
        .reset_index(
            drop=True
        )
    )

    # Keep requested range only.
    df = df[
        (df["time"] >= int(start_time))
        & (df["time"] <= int(end_time))
    ].copy()

    # Convert Unix seconds -> UTC timestamp.
    df["time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True
    )

    # Numeric OHLCV.
    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = (
        df
        .dropna(
            subset=[
                "time",
                "open",
                "high",
                "low",
                "close",
            ]
        )
        .reset_index(
            drop=True
        )
    )

    return df


# ============================================================
# RISK
# ============================================================

def calculate_risk(
    direction,
    entry,
    sl
):

    if direction == "LONG":

        return entry - sl

    return sl - entry


# ============================================================
# TARGET
# ============================================================

def calculate_target(
    direction,
    entry,
    risk,
    multiple
):

    if direction == "LONG":

        return entry + (
            risk * multiple
        )

    return entry - (
        risk * multiple
    )


# ============================================================
# TRADE REPLAY
# ============================================================

def replay_trade(
    candles,
    direction,
    entry,
    sl,
    entry_time
):

    risk = calculate_risk(
        direction,
        entry,
        sl
    )

    # --------------------------------------------------------
    # Invalid SL
    # --------------------------------------------------------

    if risk <= 0:

        return {
            "result": "INVALID_SL",
            "exit_time": None,
            "exit_price": None,
            "r_multiple": None,
            "mae": None,
            "mfe": None,
            "risk": risk,
            "tp2": None,
            "tp3": None,
        }

    # --------------------------------------------------------
    # Targets
    # --------------------------------------------------------

    tp2 = calculate_target(
        direction,
        entry,
        risk,
        2
    )

    tp3 = calculate_target(
        direction,
        entry,
        risk,
        3
    )

    # --------------------------------------------------------
    # Candles after entry
    # --------------------------------------------------------

    future = candles[
        candles["time"] > entry_time
    ].copy()

    if future.empty:

        return {
            "result": "NO_DATA",
            "exit_time": None,
            "exit_price": None,
            "r_multiple": None,
            "mae": None,
            "mfe": None,
            "risk": risk,
            "tp2": tp2,
            "tp3": tp3,
        }

    # --------------------------------------------------------
    # MAE / MFE
    # --------------------------------------------------------

    mae = 0.0
    mfe = 0.0

    # --------------------------------------------------------
    # Replay candle by candle
    # --------------------------------------------------------

    for _, candle in future.iterrows():

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        candle_time = candle["time"]

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if direction == "LONG":

            adverse = (
                entry - low
            )

            favorable = (
                high - entry
            )

            sl_hit = (
                low <= sl
            )

            tp2_hit = (
                high >= tp2
            )

            tp3_hit = (
                high >= tp3
            )

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

        else:

            adverse = (
                high - entry
            )

            favorable = (
                entry - low
            )

            sl_hit = (
                high >= sl
            )

            tp2_hit = (
                low <= tp2
            )

            tp3_hit = (
                low <= tp3
            )

        # ----------------------------------------------------
        # MAE / MFE
        # ----------------------------------------------------

        mae = max(
            mae,
            adverse
        )

        mfe = max(
            mfe,
            favorable
        )

        # ----------------------------------------------------
        # SAME-CANDLE RULE
        #
        # If SL and TP are both touched in the same 5M
        # candle, count SL FIRST.
        # ----------------------------------------------------

        if sl_hit:

            return {
                "result": "SL",
                "exit_time": candle_time,
                "exit_price": sl,
                "r_multiple": -1.0,
                "mae": mae,
                "mfe": mfe,
                "risk": risk,
                "tp2": tp2,
                "tp3": tp3,
            }

        # ----------------------------------------------------
        # TP3
        # ----------------------------------------------------

        if tp3_hit:

            return {
                "result": "TP3",
                "exit_time": candle_time,
                "exit_price": tp3,
                "r_multiple": 3.0,
                "mae": mae,
                "mfe": mfe,
                "risk": risk,
                "tp2": tp2,
                "tp3": tp3,
            }

        # ----------------------------------------------------
        # TP2
        # ----------------------------------------------------

        if tp2_hit:

            return {
                "result": "TP2",
                "exit_time": candle_time,
                "exit_price": tp2,
                "r_multiple": 2.0,
                "mae": mae,
                "mfe": mfe,
                "risk": risk,
                "tp2": tp2,
                "tp3": tp3,
            }

    # --------------------------------------------------------
    # No SL / TP hit
    # --------------------------------------------------------

    return {
        "result": "NO_OUTCOME",
        "exit_time": None,
        "exit_price": None,
        "r_multiple": None,
        "mae": mae,
        "mfe": mfe,
        "risk": risk,
        "tp2": tp2,
        "tp3": tp3,
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("STEP 9.4 — SL / TP OUTCOME REPLAY")
    print("=" * 70)

    all_results = []

    # ========================================================
    # CASE LOOP
    # ========================================================

    for case in CASES:

        print()
        print("=" * 70)

        print(
            f"{case['case_id']} | "
            f"{case['direction']} | "
            f"Entry {case['entry_time']}"
        )

        print("=" * 70)

        entry_time = parse_time(
            case["entry_time"]
        )

        start_time = int(
            entry_time.timestamp()
        )

        end_time = (
            start_time
            + REPLAY_CANDLES * 300
        )

        # ----------------------------------------------------
        # FETCH
        # ----------------------------------------------------

        print(
            "Fetching 5M MEXC candles..."
        )

        candles = fetch_5m_candles(
            start_time,
            end_time
        )

        print(
            f"Candles fetched: "
            f"{len(candles)}"
        )

        # ----------------------------------------------------
        # NO DATA
        # ----------------------------------------------------

        if candles.empty:

            print(
                "WARNING: No candles available."
            )

            continue

        # ----------------------------------------------------
        # RETEST WICK
        # ----------------------------------------------------

        wick_sl = case[
            "retest_wick_sl"
        ]

        wick_result = replay_trade(
            candles=candles,
            direction=case["direction"],
            entry=case["entry"],
            sl=wick_sl,
            entry_time=entry_time,
        )

        all_results.append({
            "case_id": case["case_id"],
            "direction": case["direction"],
            "entry_time": entry_time,
            "entry": case["entry"],
            "sl_model": "RETEST_WICK",
            "sl": wick_sl,
            **wick_result,
        })

        # ----------------------------------------------------
        # 5M STRUCTURE
        # ----------------------------------------------------

        structure_sl = case[
            "structure_sl"
        ]

        structure_result = replay_trade(
            candles=candles,
            direction=case["direction"],
            entry=case["entry"],
            sl=structure_sl,
            entry_time=entry_time,
        )

        all_results.append({
            "case_id": case["case_id"],
            "direction": case["direction"],
            "entry_time": entry_time,
            "entry": case["entry"],
            "sl_model": "5M_STRUCTURE",
            "sl": structure_sl,
            **structure_result,
        })

        # ----------------------------------------------------
        # PRINT RESULTS
        # ----------------------------------------------------

        print()
        print("RETEST WICK SL")
        print(
            f"  Entry : "
            f"{case['entry']:.4f}"
        )
        print(
            f"  SL    : "
            f"{wick_sl:.4f}"
        )
        print(
            f"  TP2   : "
            f"{wick_result.get('tp2')}"
        )
        print(
            f"  TP3   : "
            f"{wick_result.get('tp3')}"
        )
        print(
            f"  Result: "
            f"{wick_result['result']}"
        )
        print(
            f"  R     : "
            f"{wick_result.get('r_multiple')}"
        )
        print(
            f"  MAE   : "
            f"{wick_result.get('mae')}"
        )
        print(
            f"  MFE   : "
            f"{wick_result.get('mfe')}"
        )
        print(
            f"  Exit  : "
            f"{wick_result.get('exit_time')}"
        )

        print()
        print("5M STRUCTURE SL")
        print(
            f"  Entry : "
            f"{case['entry']:.4f}"
        )
        print(
            f"  SL    : "
            f"{structure_sl:.4f}"
        )
        print(
            f"  TP2   : "
            f"{structure_result.get('tp2')}"
        )
        print(
            f"  TP3   : "
            f"{structure_result.get('tp3')}"
        )
        print(
            f"  Result: "
            f"{structure_result['result']}"
        )
        print(
            f"  R     : "
            f"{structure_result.get('r_multiple')}"
        )
        print(
            f"  MAE   : "
            f"{structure_result.get('mae')}"
        )
        print(
            f"  MFE   : "
            f"{structure_result.get('mfe')}"
        )
        print(
            f"  Exit  : "
            f"{structure_result.get('exit_time')}"
        )

    # ========================================================
    # CASE OUTPUT
    # ========================================================

    results_df = pd.DataFrame(
        all_results
    )

    results_path = (
        "step9_4_sl_tp_outcome_cases.csv"
    )

    results_df.to_csv(
        results_path,
        index=False
    )

    # ========================================================
    # SUMMARY
    # ========================================================

    summary_rows = []

    for model in [
        "RETEST_WICK",
        "5M_STRUCTURE",
    ]:

        subset = results_df[
            results_df["sl_model"] == model
        ].copy()

        if subset.empty:
            continue

        r_values = (
            subset["r_multiple"]
            .dropna()
        )

        total_cases = len(
            subset
        )

        tp2 = int(
            (
                subset["result"]
                == "TP2"
            ).sum()
        )

        tp3 = int(
            (
                subset["result"]
                == "TP3"
            ).sum()
        )

        sl = int(
            (
                subset["result"]
                == "SL"
            ).sum()
        )

        no_outcome = int(
            (
                subset["result"]
                == "NO_OUTCOME"
            ).sum()
        )

        no_data = int(
            (
                subset["result"]
                == "NO_DATA"
            ).sum()
        )

        invalid_sl = int(
            (
                subset["result"]
                == "INVALID_SL"
            ).sum()
        )

        total_r = (
            r_values.sum()
            if not r_values.empty
            else None
        )

        avg_r = (
            r_values.mean()
            if not r_values.empty
            else None
        )

        summary_rows.append({
            "sl_model": model,
            "total_cases": total_cases,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "no_outcome": no_outcome,
            "no_data": no_data,
            "invalid_sl": invalid_sl,
            "total_r": total_r,
            "avg_r": avg_r,
        })

    summary_df = pd.DataFrame(
        summary_rows
    )

    summary_path = (
        "step9_4_sl_tp_outcome_summary.csv"
    )

    summary_df.to_csv(
        summary_path,
        index=False
    )

    # ========================================================
    # FINAL
    # ========================================================

    print()
    print("=" * 70)
    print("STEP 9.4 SUMMARY")
    print("=" * 70)

    if summary_df.empty:

        print(
            "No replay results generated."
        )

    else:

        print(
            summary_df.to_string(
                index=False
            )
        )

    print()
    print(
        f"Created: {results_path}"
    )

    print(
        f"Created: {summary_path}"
    )

    print()
    print("=" * 70)
    print("STEP 9.4 COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
