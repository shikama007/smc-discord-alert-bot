import requests
import pandas as pd

# ==========================================
# SETTINGS
# ==========================================

SYMBOL = "LTC_USDT"
TIMEFRAME = "Min60"
CANDLE_LIMIT = 100

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"


# ==========================================
# GET MEXC FUTURES DATA
# ==========================================

def get_candles(symbol, interval, limit=100):

    url = f"{BASE_URL}/{symbol}"

    response = requests.get(
        url,
        params={"interval": interval},
        timeout=10
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("success"):
        raise RuntimeError(
            f"MEXC API Error: {result}"
        )

    data = result["data"]

    df = pd.DataFrame({
        "time": data["time"],
        "open": data["open"],
        "high": data["high"],
        "low": data["low"],
        "close": data["close"],
        "volume": data["vol"]
    })

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df["time"] = pd.to_datetime(
        df["time"],
        unit="s"
    )

    df = df.tail(limit).reset_index(drop=True)

    return df


# ==========================================
# FIND SWING HIGHS / LOWS
# ==========================================

def find_swings(df, strength=2):

    swing_highs = []
    swing_lows = []

    for i in range(
        strength,
        len(df) - strength
    ):

        current_high = df.loc[i, "high"]
        current_low = df.loc[i, "low"]

        left_highs = df.loc[
            i-strength:i-1,
            "high"
        ]

        right_highs = df.loc[
            i+1:i+strength,
            "high"
        ]

        left_lows = df.loc[
            i-strength:i-1,
            "low"
        ]

        right_lows = df.loc[
            i+1:i+strength,
            "low"
        ]

        # Swing High
        if (
            current_high > left_highs.max()
            and
            current_high > right_highs.max()
        ):
            swing_highs.append({
                "index": i,
                "price": current_high,
                "time": df.loc[i, "time"]
            })

        # Swing Low
        if (
            current_low < left_lows.min()
            and
            current_low < right_lows.min()
        ):
            swing_lows.append({
                "index": i,
                "price": current_low,
                "time": df.loc[i, "time"]
            })

    return swing_highs, swing_lows


# ==========================================
# MARKET STRUCTURE
# ==========================================

def analyze_structure(
    swing_highs,
    swing_lows
):

    if len(swing_highs) < 2 or len(swing_lows) < 2:

        return {
            "trend": "UNKNOWN",
            "structure": "NOT ENOUGH DATA",
            "bos": "NONE",
            "choch": "NONE"
        }

    last_high = swing_highs[-1]
    previous_high = swing_highs[-2]

    last_low = swing_lows[-1]
    previous_low = swing_lows[-2]

    # --------------------------------------
    # Higher High / Lower High
    # --------------------------------------

    higher_high = (
        last_high["price"]
        > previous_high["price"]
    )

    lower_high = (
        last_high["price"]
        < previous_high["price"]
    )

    # --------------------------------------
    # Higher Low / Lower Low
    # --------------------------------------

    higher_low = (
        last_low["price"]
        > previous_low["price"]
    )

    lower_low = (
        last_low["price"]
        < previous_low["price"]
    )

    # --------------------------------------
    # BULLISH STRUCTURE
    # --------------------------------------

    if higher_high and higher_low:

        return {
            "trend": "BULLISH",
            "structure": "HH → HL",
            "bos": "BULLISH",
            "choch": "NONE"
        }

    # --------------------------------------
    # BEARISH STRUCTURE
    # --------------------------------------

    if lower_high and lower_low:

        return {
            "trend": "BEARISH",
            "structure": "LH → LL",
            "bos": "BEARISH",
            "choch": "NONE"
        }

    # --------------------------------------
    # RANGE / MIXED
    # --------------------------------------

    return {
        "trend": "RANGE",
        "structure": "MIXED",
        "bos": "NONE",
        "choch": "NONE"
    }


# ==========================================
# MAIN
# ==========================================

print("\n🚀 SMC ENGINE — STEP 1")
print("==============================")

print(
    f"📌 Symbol: {SYMBOL}"
)

print(
    f"⏱️ Timeframe: {TIMEFRAME}"
)

print(
    "📡 Downloading MEXC Futures data..."
)


df = get_candles(
    SYMBOL,
    TIMEFRAME,
    CANDLE_LIMIT
)


print(
    f"✅ {len(df)} candles loaded"
)


# ==========================================
# SWINGS
# ==========================================

swing_highs, swing_lows = find_swings(
    df,
    strength=2
)


print(
    f"\n🔺 Swing Highs: {len(swing_highs)}"
)

print(
    f"🔻 Swing Lows: {len(swing_lows)}"
)


# ==========================================
# SHOW RECENT SWINGS
# ==========================================

print("\n📍 RECENT SWING HIGHS")

for swing in swing_highs[-5:]:

    print(
        f"   {swing['time']} → "
        f"${swing['price']:.4f}"
    )


print("\n📍 RECENT SWING LOWS")

for swing in swing_lows[-5:]:

    print(
        f"   {swing['time']} → "
        f"${swing['price']:.4f}"
    )


# ==========================================
# STRUCTURE ANALYSIS
# ==========================================

structure = analyze_structure(
    swing_highs,
    swing_lows
)


print("\n==============================")

print(
    f"📊 MARKET TREND: "
    f"{structure['trend']}"
)

print(
    f"🏗️ STRUCTURE: "
    f"{structure['structure']}"
)

print(
    f"🚀 BOS: "
    f"{structure['bos']}"
)

print(
    f"🔄 CHOCH: "
    f"{structure['choch']}"
)

print("==============================")

print("\n✅ STEP 1 MARKET STRUCTURE TEST COMPLETE!")
