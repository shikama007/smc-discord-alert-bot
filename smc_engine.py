import requests
import pandas as pd
import os

# ==========================================
# SETTINGS
# ==========================================

SYMBOL = "LTC_USDT"

TIMEFRAME = "Min60"
CANDLE_LIMIT = 100

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

# Equal High / Equal Low tolerance
# 0.001 = 0.1%
EQUAL_TOLERANCE = 0.001

# Discord
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


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
                "price": float(current_high),
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
                "price": float(current_low),
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

    higher_high = (
        last_high["price"]
        > previous_high["price"]
    )

    lower_high = (
        last_high["price"]
        < previous_high["price"]
    )

    higher_low = (
        last_low["price"]
        > previous_low["price"]
    )

    lower_low = (
        last_low["price"]
        < previous_low["price"]
    )

    # Bullish structure
    if higher_high and higher_low:

        return {
            "trend": "BULLISH",
            "structure": "HH → HL",
            "bos": "BULLISH",
            "choch": "NONE"
        }

    # Bearish structure
    if lower_high and lower_low:

        return {
            "trend": "BEARISH",
            "structure": "LH → LL",
            "bos": "BEARISH",
            "choch": "NONE"
        }

    # Range / Mixed
    return {
        "trend": "RANGE",
        "structure": "MIXED",
        "bos": "NONE",
        "choch": "NONE"
    }


# ==========================================
# EQUAL HIGH / LOW CHECK
# ==========================================

def prices_are_equal(price1, price2):

    difference = abs(price1 - price2)

    average = (price1 + price2) / 2

    if average == 0:
        return False

    percentage_difference = (
        difference / average
    )

    return percentage_difference <= EQUAL_TOLERANCE


# ==========================================
# FIND EQUAL HIGHS
# ==========================================

def find_equal_highs(swing_highs):

    equal_highs = []

    if len(swing_highs) < 2:
        return equal_highs

    for i in range(len(swing_highs) - 1):

        first = swing_highs[i]
        second = swing_highs[i + 1]

        if prices_are_equal(
            first["price"],
            second["price"]
        ):

            average_price = (
                first["price"]
                + second["price"]
            ) / 2

            equal_highs.append({
                "price": average_price,
                "first_time": first["time"],
                "second_time": second["time"]
            })

    return equal_highs


# ==========================================
# FIND EQUAL LOWS
# ==========================================

def find_equal_lows(swing_lows):

    equal_lows = []

    if len(swing_lows) < 2:
        return equal_lows

    for i in range(len(swing_lows) - 1):

        first = swing_lows[i]
        second = swing_lows[i + 1]

        if prices_are_equal(
            first["price"],
            second["price"]
        ):

            average_price = (
                first["price"]
                + second["price"]
            ) / 2

            equal_lows.append({
                "price": average_price,
                "first_time": first["time"],
                "second_time": second["time"]
            })

    return equal_lows


# ==========================================
# LIQUIDITY ANALYSIS
# ==========================================

def analyze_liquidity(
    df,
    swing_highs,
    swing_lows
):

    current_price = float(
        df["close"].iloc[-1]
    )

    # --------------------------------------
    # Buy-side liquidity
    # --------------------------------------

    buy_side_levels = []

    for swing in swing_highs[-5:]:

        if swing["price"] > current_price:

            buy_side_levels.append(
                swing["price"]
            )

    # --------------------------------------
    # Sell-side liquidity
    # --------------------------------------

    sell_side_levels = []

    for swing in swing_lows[-5:]:

        if swing["price"] < current_price:

            sell_side_levels.append(
                swing["price"]
            )

    # --------------------------------------
    # Equal Highs / Equal Lows
    # --------------------------------------

    equal_highs = find_equal_highs(
        swing_highs
    )

    equal_lows = find_equal_lows(
        swing_lows
    )

    # --------------------------------------
    # PDH / PDL
    # --------------------------------------

    df["date"] = df["time"].dt.date

    unique_dates = df["date"].unique()

    pdh = None
    pdl = None

    if len(unique_dates) >= 2:

        previous_date = unique_dates[-2]

        previous_day = df[
            df["date"] == previous_date
        ]

        if not previous_day.empty:

            pdh = float(
                previous_day["high"].max()
            )

            pdl = float(
                previous_day["low"].min()
            )

    return {
        "current_price": current_price,
        "buy_side_levels": buy_side_levels,
        "sell_side_levels": sell_side_levels,
        "equal_highs": equal_highs,
        "equal_lows": equal_lows,
        "pdh": pdh,
        "pdl": pdl
    }


# ==========================================
# SEND DISCORD MESSAGE
# ==========================================

def send_discord_message(message):

    if not WEBHOOK_URL:

        raise RuntimeError(
            "DISCORD_WEBHOOK_URL secret not found!"
        )

    payload = {
        "content": message
    }

    response = requests.post(
        WEBHOOK_URL,
        json=payload,
        timeout=10
    )

    if response.status_code not in (200, 204):

        print(
            f"❌ Discord Error: "
            f"{response.status_code}"
        )

        print(response.text)

        response.raise_for_status()

    print(
        "✅ Discord message sent successfully!"
    )


# ==========================================
# MAIN
# ==========================================

print("\n🚀 SMC ENGINE — STEP 2")
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


# ==========================================
# GET DATA
# ==========================================

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
    f"\n🔺 Swing Highs: "
    f"{len(swing_highs)}"
)

print(
    f"🔻 Swing Lows: "
    f"{len(swing_lows)}"
)


# ==========================================
# MARKET STRUCTURE
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


# ==========================================
# LIQUIDITY
# ==========================================

liquidity = analyze_liquidity(
    df,
    swing_highs,
    swing_lows
)

print("\n💧 LIQUIDITY")
print("------------------------------")

print(
    f"💰 Current Price: "
    f"${liquidity['current_price']:.4f}"
)


# Equal Highs
if liquidity["equal_highs"]:

    print("\n🔺 EQUAL HIGHS (EQH)")

    for level in liquidity["equal_highs"][-5:]:

        print(
            f"   ${level['price']:.4f}"
        )

else:

    print("\n🔺 EQUAL HIGHS: None")


# Equal Lows
if liquidity["equal_lows"]:

    print("\n🔻 EQUAL LOWS (EQL)")

    for level in liquidity["equal_lows"][-5:]:

        print(
            f"   ${level['price']:.4f}"
        )

else:

    print("\n🔻 EQUAL LOWS: None")


# Buy-side liquidity
print("\n🟢 BUY-SIDE LIQUIDITY (BSL)")

if liquidity["buy_side_levels"]:

    for level in liquidity["buy_side_levels"][-5:]:

        print(
            f"   ${level:.4f}"
        )

else:

    print("   None")


# Sell-side liquidity
print("\n🔴 SELL-SIDE LIQUIDITY (SSL)")

if liquidity["sell_side_levels"]:

    for level in liquidity["sell_side_levels"][-5:]:

        print(
            f"   ${level:.4f}"
        )

else:

    print("   None")


# PDH / PDL
print("\n📅 PREVIOUS DAY LEVELS")

if liquidity["pdh"] is not None:

    print(
        f"   PDH: ${liquidity['pdh']:.4f}"
    )

else:

    print("   PDH: None")


if liquidity["pdl"] is not None:

    print(
        f"   PDL: ${liquidity['pdl']:.4f}"
    )

else:

    print("   PDL: None")


print("==============================")


# ==========================================
# DISCORD MESSAGE
# ==========================================

message = (
    "📊 **LTCUSDT.P — ICT STEP 2**\n\n"

    f"💰 Current Price: "
    f"`${liquidity['current_price']:.4f}`\n"

    f"⏱️ Timeframe: `1H`\n\n"

    "🏗️ **MARKET STRUCTURE**\n"
    f"📈 Trend: **{structure['trend']}**\n"
    f"Structure: **{structure['structure']}**\n"
    f"BOS: **{structure['bos']}**\n"
    f"CHOCH: **{structure['choch']}**\n\n"

    "💧 **LIQUIDITY**\n"
)

# EQH
if liquidity["equal_highs"]:

    message += "\n🔺 **EQH**\n"

    for level in liquidity["equal_highs"][-3:]:

        message += (
            f"• `${level['price']:.4f}`\n"
        )

else:

    message += "\n🔺 EQH: `None detected`\n"


# EQL
if liquidity["equal_lows"]:

    message += "\n🔻 **EQL**\n"

    for level in liquidity["equal_lows"][-3:]:

        message += (
            f"• `${level['price']:.4f}`\n"
        )

else:

    message += "\n🔻 EQL: `None detected`\n"


# BSL
if liquidity["buy_side_levels"]:

    message += "\n🟢 **BSL**\n"

    for level in liquidity["buy_side_levels"][-3:]:

        message += (
            f"• `${level:.4f}`\n"
        )

else:

    message += "\n🟢 BSL: `None`\n"


# SSL
if liquidity["sell_side_levels"]:

    message += "\n🔴 **SSL**\n"

    for level in liquidity["sell_side_levels"][-3:]:

        message += (
            f"• `${level:.4f}`\n"
        )

else:

    message += "\n🔴 SSL: `None`\n"


# PDH / PDL
message += "\n📅 **PREVIOUS DAY LEVELS**\n"

if liquidity["pdh"] is not None:

    message += (
        f"PDH: `${liquidity['pdh']:.4f}`\n"
    )

else:

    message += "PDH: `None`\n"


if liquidity["pdl"] is not None:

    message += (
        f"PDL: `${liquidity['pdl']:.4f}`\n"
    )

else:

    message += "PDL: `None`\n"


message += (
    "\n🤖 **ICT/SMC Engine — Step 2**\n"
    "✅ Market Structure + Liquidity Scan Complete"
)


# ==========================================
# SEND TO DISCORD
# ==========================================

print("\n📡 Sending Step 2 result to Discord...")

send_discord_message(message)


print(
    "\n✅ STEP 2 MARKET STRUCTURE + "
    "LIQUIDITY TEST COMPLETE!"
)
