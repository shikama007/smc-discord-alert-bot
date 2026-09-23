import requests
import pandas as pd
import os


# =========================================================
# SETTINGS
# =========================================================

SYMBOL = "LTC_USDT"

TIMEFRAME = "Min60"
CANDLE_LIMIT = 150

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

# Equal High / Equal Low tolerance
# 0.001 = 0.1%
EQUAL_TOLERANCE = 0.001

# Swing strength
SWING_STRENGTH = 2

# Discord
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


# =========================================================
# GET MEXC FUTURES DATA
# =========================================================

def get_candles(symbol, interval, limit=150):

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

    df = df.dropna().reset_index(drop=True)

    return df.tail(limit).reset_index(drop=True)


# =========================================================
# FIND CONFIRMED SWINGS
# =========================================================

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

        # -----------------------------
        # Swing High
        # -----------------------------

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

        # -----------------------------
        # Swing Low
        # -----------------------------

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


# =========================================================
# STRUCTURE LABELS
# =========================================================

def get_structure_labels(
    swing_highs,
    swing_lows
):

    high_labels = []
    low_labels = []

    # -----------------------------
    # High labels
    # -----------------------------

    for i in range(1, len(swing_highs)):

        current = swing_highs[i]["price"]
        previous = swing_highs[i - 1]["price"]

        if current > previous:

            label = "HH"

        elif current < previous:

            label = "LH"

        else:

            label = "EQH"

        high_labels.append({
            "label": label,
            "price": current,
            "time": swing_highs[i]["time"],
            "index": swing_highs[i]["index"]
        })

    # -----------------------------
    # Low labels
    # -----------------------------

    for i in range(1, len(swing_lows)):

        current = swing_lows[i]["price"]
        previous = swing_lows[i - 1]["price"]

        if current > previous:

            label = "HL"

        elif current < previous:

            label = "LL"

        else:

            label = "EQL"

        low_labels.append({
            "label": label,
            "price": current,
            "time": swing_lows[i]["time"],
            "index": swing_lows[i]["index"]
        })

    return high_labels, low_labels


# =========================================================
# DETERMINE STRUCTURAL BIAS
# =========================================================

def determine_bias(
    swing_highs,
    swing_lows
):

    if len(swing_highs) < 2 or len(swing_lows) < 2:

        return "UNKNOWN"

    last_high = swing_highs[-1]["price"]
    previous_high = swing_highs[-2]["price"]

    last_low = swing_lows[-1]["price"]
    previous_low = swing_lows[-2]["price"]

    higher_high = last_high > previous_high
    lower_high = last_high < previous_high

    higher_low = last_low > previous_low
    lower_low = last_low < previous_low

    # Bullish structure
    if higher_high and higher_low:

        return "BULLISH"

    # Bearish structure
    if lower_high and lower_low:

        return "BEARISH"

    return "RANGE"


# =========================================================
# DETECT BOS / CHOCH
# =========================================================

def detect_structure_event(
    df,
    swing_highs,
    swing_lows
):

    result = {
        "event": "NONE",
        "direction": "NONE",
        "broken_level": None,
        "broken_level_type": None,
        "break_time": None
    }

    if len(swing_highs) < 2 or len(swing_lows) < 2:

        return result

    # -----------------------------------------
    # Current structural bias
    # -----------------------------------------

    bias = determine_bias(
        swing_highs,
        swing_lows
    )

    # -----------------------------------------
    # Most recent confirmed swing levels
    # -----------------------------------------

    last_high = swing_highs[-1]
    last_low = swing_lows[-1]

    last_high_price = last_high["price"]
    last_low_price = last_low["price"]

    # -----------------------------------------
    # Use CLOSED candles only
    #
    # Last candle may still be forming.
    # -----------------------------------------

    closed_df = df.iloc[:-1].copy()

    if closed_df.empty:
        return result

    current_close = float(
        closed_df["close"].iloc[-1]
    )

    current_time = closed_df["time"].iloc[-1]

    # =====================================================
    # BULLISH BIAS
    # =====================================================

    if bias == "BULLISH":

        # Price breaks previous structural high
        if current_close > last_high_price:

            result = {
                "event": "BOS",
                "direction": "BULLISH",
                "broken_level": last_high_price,
                "broken_level_type": "SWING HIGH",
                "break_time": current_time
            }

            return result

        # Price breaks structural low
        # = possible trend change
        if current_close < last_low_price:

            result = {
                "event": "CHOCH",
                "direction": "BEARISH",
                "broken_level": last_low_price,
                "broken_level_type": "SWING LOW",
                "break_time": current_time
            }

            return result

    # =====================================================
    # BEARISH BIAS
    # =====================================================

    if bias == "BEARISH":

        # Price breaks previous structural low
        if current_close < last_low_price:

            result = {
                "event": "BOS",
                "direction": "BEARISH",
                "broken_level": last_low_price,
                "broken_level_type": "SWING LOW",
                "break_time": current_time
            }

            return result

        # Price breaks structural high
        # = possible trend change
        if current_close > last_high_price:

            result = {
                "event": "CHOCH",
                "direction": "BULLISH",
                "broken_level": last_high_price,
                "broken_level_type": "SWING HIGH",
                "break_time": current_time
            }

            return result

    return result


# =========================================================
# EQUAL HIGH / LOW
# =========================================================

def prices_are_equal(price1, price2):

    difference = abs(price1 - price2)

    average = (price1 + price2) / 2

    if average == 0:
        return False

    percentage_difference = (
        difference / average
    )

    return percentage_difference <= EQUAL_TOLERANCE


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

            price = (
                first["price"]
                + second["price"]
            ) / 2

            equal_highs.append(price)

    return equal_highs


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

            price = (
                first["price"]
                + second["price"]
            ) / 2

            equal_lows.append(price)

    return equal_lows


# =========================================================
# LIQUIDITY ANALYSIS
# =========================================================

def analyze_liquidity(
    df,
    swing_highs,
    swing_lows
):

    current_price = float(
        df["close"].iloc[-1]
    )

    # -----------------------------------------
    # BSL
    # -----------------------------------------

    bsl = []

    for swing in swing_highs[-10:]:

        if swing["price"] > current_price:

            bsl.append(
                swing["price"]
            )

    # -----------------------------------------
    # SSL
    # -----------------------------------------

    ssl = []

    for swing in swing_lows[-10:]:

        if swing["price"] < current_price:

            ssl.append(
                swing["price"]
            )

    # -----------------------------------------
    # EQH / EQL
    # -----------------------------------------

    eqh = find_equal_highs(
        swing_highs
    )

    eql = find_equal_lows(
        swing_lows
    )

    # -----------------------------------------
    # PDH / PDL
    # -----------------------------------------

    temp_df = df.copy()

    temp_df["date"] = (
        temp_df["time"].dt.date
    )

    dates = temp_df["date"].unique()

    pdh = None
    pdl = None

    if len(dates) >= 2:

        previous_date = dates[-2]

        previous_day = temp_df[
            temp_df["date"] == previous_date
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
        "bsl": bsl,
        "ssl": ssl,
        "eqh": eqh,
        "eql": eql,
        "pdh": pdh,
        "pdl": pdl
    }


# =========================================================
# DISCORD
# =========================================================

def send_discord_message(message):

    if not WEBHOOK_URL:

        raise RuntimeError(
            "DISCORD_WEBHOOK_URL secret not found!"
        )

    response = requests.post(
        WEBHOOK_URL,
        json={
            "content": message
        },
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


# =========================================================
# MAIN
# =========================================================

print("\n🚀 ICT/SMC ENGINE — STRUCTURE UPGRADE")
print("========================================")

print(
    f"📌 Symbol: {SYMBOL}"
)

print(
    f"⏱️ Timeframe: 1H"
)

print(
    "📡 Downloading MEXC Futures data..."
)


# =========================================================
# DATA
# =========================================================

df = get_candles(
    SYMBOL,
    TIMEFRAME,
    CANDLE_LIMIT
)

print(
    f"✅ {len(df)} candles loaded"
)


# =========================================================
# SWINGS
# =========================================================

swing_highs, swing_lows = find_swings(
    df,
    SWING_STRENGTH
)

print(
    f"\n🔺 Swing Highs: {len(swing_highs)}"
)

print(
    f"🔻 Swing Lows: {len(swing_lows)}"
)


# =========================================================
# STRUCTURE LABELS
# =========================================================

high_labels, low_labels = get_structure_labels(
    swing_highs,
    swing_lows
)


# =========================================================
# BIAS
# =========================================================

bias = determine_bias(
    swing_highs,
    swing_lows
)


# =========================================================
# STRUCTURE EVENT
# =========================================================

event = detect_structure_event(
    df,
    swing_highs,
    swing_lows
)


# =========================================================
# LIQUIDITY
# =========================================================

liquidity = analyze_liquidity(
    df,
    swing_highs,
    swing_lows
)


# =========================================================
# TERMINAL OUTPUT
# =========================================================

print("\n========================================")

print(
    f"📊 MARKET BIAS: {bias}"
)

print("\n🏗️ RECENT STRUCTURE")

if high_labels:

    print(
        f"🔺 High: "
        f"{high_labels[-1]['label']} "
        f"@ ${high_labels[-1]['price']:.4f}"
    )

if low_labels:

    print(
        f"🔻 Low: "
        f"{low_labels[-1]['label']} "
        f"@ ${low_labels[-1]['price']:.4f}"
    )


print("\n🚀 STRUCTURE EVENT")

print(
    f"Event: {event['event']}"
)

print(
    f"Direction: {event['direction']}"
)

if event["broken_level"] is not None:

    print(
        f"Broken Level: "
        f"${event['broken_level']:.4f}"
    )

    print(
        f"Level Type: "
        f"{event['broken_level_type']}"
    )

else:

    print(
        "Broken Level: None"
    )


print("\n💧 LIQUIDITY")

print(
    f"💰 Current Price: "
    f"${liquidity['current_price']:.4f}"
)


if liquidity["eqh"]:

    print("\n🔺 EQH")

    for price in liquidity["eqh"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print("\n🔺 EQH: None")


if liquidity["eql"]:

    print("\n🔻 EQL")

    for price in liquidity["eql"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print("\n🔻 EQL: None")


if liquidity["bsl"]:

    print("\n🟢 BSL")

    for price in liquidity["bsl"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print("\n🟢 BSL: None")


if liquidity["ssl"]:

    print("\n🔴 SSL")

    for price in liquidity["ssl"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print("\n🔴 SSL: None")


print("\n📅 PREVIOUS DAY")

print(
    f"PDH: "
    f"${liquidity['pdh']:.4f}"
    if liquidity["pdh"] is not None
    else "PDH: None"
)

print(
    f"PDL: "
    f"${liquidity['pdl']:.4f}"
    if liquidity["pdl"] is not None
    else "PDL: None"
)

print("\n========================================")


# =========================================================
# DISCORD MESSAGE
# =========================================================

message = (
    "🧠 **LTCUSDT.P — ICT/SMC STRUCTURE UPDATE**\n\n"

    f"💰 Current Price: "
    f"`${liquidity['current_price']:.4f}`\n"

    "⏱️ Timeframe: `1H`\n\n"

    "🏗️ **MARKET STRUCTURE**\n"
    f"📊 Bias: **{bias}**\n"
)


# Recent high / low labels

if high_labels:

    message += (
        f"🔺 High Structure: "
        f"**{high_labels[-1]['label']}** "
        f"@ `${high_labels[-1]['price']:.4f}`\n"
    )

if low_labels:

    message += (
        f"🔻 Low Structure: "
        f"**{low_labels[-1]['label']}** "
        f"@ `${low_labels[-1]['price']:.4f}`\n"
    )


# Structure event

message += "\n🚨 **STRUCTURE EVENT**\n"

message += (
    f"Event: **{event['event']}**\n"
    f"Direction: **{event['direction']}**\n"
)


if event["broken_level"] is not None:

    message += (
        f"Broken Level: "
        f"`${event['broken_level']:.4f}`\n"
        f"Level: `{event['broken_level_type']}`\n"
    )

else:

    message += (
        "Broken Level: `None`\n"
    )


# Liquidity

message += "\n💧 **LIQUIDITY**\n"


# EQH

if liquidity["eqh"]:

    message += "\n🔺 **EQH**\n"

    for price in liquidity["eqh"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🔺 EQH: `None`\n"
    )


# EQL

if liquidity["eql"]:

    message += "\n🔻 **EQL**\n"

    for price in liquidity["eql"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🔻 EQL: `None`\n"
    )


# BSL

if liquidity["bsl"]:

    message += "\n🟢 **BSL**\n"

    for price in liquidity["bsl"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🟢 BSL: `None`\n"
    )


# SSL

if liquidity["ssl"]:

    message += "\n🔴 **SSL**\n"

    for price in liquidity["ssl"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🔴 SSL: `None`\n"
    )


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
    "\n🤖 **ICT/SMC Engine**\n"
    "✅ Structure + Liquidity Scan Complete"
)


# =========================================================
# SEND DISCORD
# =========================================================

print(
    "\n📡 Sending upgraded analysis to Discord..."
)

send_discord_message(message)


print(
    "\n✅ STRUCTURE UPGRADE TEST COMPLETE!"
)
