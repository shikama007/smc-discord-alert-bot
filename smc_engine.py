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

SWING_STRENGTH = 2

# EQH / EQL tolerance
# 0.001 = 0.1%
EQUAL_TOLERANCE = 0.001

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

    df = df.dropna()

    df = df.tail(limit).reset_index(drop=True)

    return df


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


# =========================================================
# HH / HL / LH / LL
# =========================================================

def label_swings(swing_highs, swing_lows):

    high_labels = []
    low_labels = []

    # -----------------------------
    # Highs
    # -----------------------------

    for i in range(1, len(swing_highs)):

        current = swing_highs[i]
        previous = swing_highs[i - 1]

        if current["price"] > previous["price"]:

            label = "HH"

        elif current["price"] < previous["price"]:

            label = "LH"

        else:

            label = "EQH"

        high_labels.append({
            "label": label,
            "price": current["price"],
            "time": current["time"],
            "index": current["index"]
        })

    # -----------------------------
    # Lows
    # -----------------------------

    for i in range(1, len(swing_lows)):

        current = swing_lows[i]
        previous = swing_lows[i - 1]

        if current["price"] > previous["price"]:

            label = "HL"

        elif current["price"] < previous["price"]:

            label = "LL"

        else:

            label = "EQL"

        low_labels.append({
            "label": label,
            "price": current["price"],
            "time": current["time"],
            "index": current["index"]
        })

    return high_labels, low_labels


# =========================================================
# INITIAL STRUCTURAL BIAS
# =========================================================

def determine_initial_bias(
    swing_highs,
    swing_lows
):

    if len(swing_highs) < 2:
        return "UNKNOWN"

    if len(swing_lows) < 2:
        return "UNKNOWN"

    last_high = swing_highs[-1]["price"]
    previous_high = swing_highs[-2]["price"]

    last_low = swing_lows[-1]["price"]
    previous_low = swing_lows[-2]["price"]

    higher_high = last_high > previous_high
    higher_low = last_low > previous_low

    lower_high = last_high < previous_high
    lower_low = last_low < previous_low

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    return "RANGE"


# =========================================================
# HISTORICAL BOS / CHOCH ENGINE
# =========================================================

def detect_historical_events(
    df,
    swing_highs,
    swing_lows
):

    events = []

    # -----------------------------------------------------
    # We only use CLOSED candles
    # -----------------------------------------------------

    closed_df = df.iloc[:-1].copy()

    if closed_df.empty:
        return events

    # -----------------------------------------------------
    # Start with structural bias from available history
    # -----------------------------------------------------

    bias = determine_initial_bias(
        swing_highs,
        swing_lows
    )

    # -----------------------------------------------------
    # Track which swing levels were already broken
    # -----------------------------------------------------

    broken_high_indices = set()
    broken_low_indices = set()

    # -----------------------------------------------------
    # Process candles chronologically
    # -----------------------------------------------------

    for candle_index in range(
        len(closed_df)
    ):

        candle = closed_df.iloc[candle_index]

        close_price = float(
            candle["close"]
        )

        candle_time = candle["time"]

        # =================================================
        # FIND MOST RECENT CONFIRMED SWING HIGH
        # BEFORE CURRENT CANDLE
        # =================================================

        available_highs = [
            swing for swing in swing_highs
            if swing["index"] < candle_index
        ]

        available_lows = [
            swing for swing in swing_lows
            if swing["index"] < candle_index
        ]

        # =================================================
        # BULLISH BREAK
        # =================================================

        if available_highs:

            latest_high = available_highs[-1]

            high_index = latest_high["index"]

            high_price = latest_high["price"]

            if (
                high_index not in broken_high_indices
                and
                close_price > high_price
            ):

                # -----------------------------
                # Determine BOS / CHOCH
                # -----------------------------

                if bias == "BEARISH":

                    event_type = "CHOCH"

                else:

                    event_type = "BOS"

                events.append({
                    "event": event_type,
                    "direction": "BULLISH",
                    "price": high_price,
                    "time": candle_time,
                    "level_type": "SWING HIGH",
                    "candle_close": close_price
                })

                broken_high_indices.add(
                    high_index
                )

                bias = "BULLISH"

        # =================================================
        # BEARISH BREAK
        # =================================================

        if available_lows:

            latest_low = available_lows[-1]

            low_index = latest_low["index"]

            low_price = latest_low["price"]

            if (
                low_index not in broken_low_indices
                and
                close_price < low_price
            ):

                # -----------------------------
                # Determine BOS / CHOCH
                # -----------------------------

                if bias == "BULLISH":

                    event_type = "CHOCH"

                else:

                    event_type = "BOS"

                events.append({
                    "event": event_type,
                    "direction": "BEARISH",
                    "price": low_price,
                    "time": candle_time,
                    "level_type": "SWING LOW",
                    "candle_close": close_price
                })

                broken_low_indices.add(
                    low_index
                )

                bias = "BEARISH"

    return events


# =========================================================
# CURRENT STRUCTURE
# =========================================================

def get_current_structure(
    swing_highs,
    swing_lows,
    events
):

    # -----------------------------------------------------
    # If historical events exist,
    # latest event gives latest directional change.
    # -----------------------------------------------------

    if events:

        latest_event = events[-1]

        if latest_event["direction"] == "BULLISH":

            bias = "BULLISH"

        elif latest_event["direction"] == "BEARISH":

            bias = "BEARISH"

        else:

            bias = "UNKNOWN"

    else:

        bias = determine_initial_bias(
            swing_highs,
            swing_lows
        )

    # -----------------------------------------------------
    # Recent structural labels
    # -----------------------------------------------------

    last_high = None
    last_low = None

    if len(swing_highs) >= 2:

        previous = swing_highs[-2]["price"]
        current = swing_highs[-1]["price"]

        if current > previous:
            label = "HH"
        elif current < previous:
            label = "LH"
        else:
            label = "EQH"

        last_high = {
            "label": label,
            "price": current,
            "time": swing_highs[-1]["time"]
        }

    if len(swing_lows) >= 2:

        previous = swing_lows[-2]["price"]
        current = swing_lows[-1]["price"]

        if current > previous:
            label = "HL"
        elif current < previous:
            label = "LL"
        else:
            label = "EQL"

        last_low = {
            "label": label,
            "price": current,
            "time": swing_lows[-1]["time"]
        }

    return {
        "bias": bias,
        "last_high": last_high,
        "last_low": last_low
    }


# =========================================================
# EQUAL HIGH / LOW
# =========================================================

def prices_are_equal(
    price1,
    price2
):

    difference = abs(
        price1 - price2
    )

    average = (
        price1 + price2
    ) / 2

    if average == 0:
        return False

    percentage_difference = (
        difference / average
    )

    return (
        percentage_difference
        <= EQUAL_TOLERANCE
    )


def find_equal_highs(swing_highs):

    levels = []

    if len(swing_highs) < 2:
        return levels

    for i in range(
        len(swing_highs) - 1
    ):

        first = swing_highs[i]
        second = swing_highs[i + 1]

        if prices_are_equal(
            first["price"],
            second["price"]
        ):

            level = (
                first["price"]
                + second["price"]
            ) / 2

            levels.append(level)

    return levels


def find_equal_lows(swing_lows):

    levels = []

    if len(swing_lows) < 2:
        return levels

    for i in range(
        len(swing_lows) - 1
    ):

        first = swing_lows[i]
        second = swing_lows[i + 1]

        if prices_are_equal(
            first["price"],
            second["price"]
        ):

            level = (
                first["price"]
                + second["price"]
            ) / 2

            levels.append(level)

    return levels


# =========================================================
# LIQUIDITY
# =========================================================

def analyze_liquidity(
    df,
    swing_highs,
    swing_lows
):

    current_price = float(
        df["close"].iloc[-1]
    )

    # -----------------------------------------------------
    # BSL
    # -----------------------------------------------------

    bsl = []

    for swing in swing_highs[-10:]:

        if swing["price"] > current_price:

            bsl.append(
                swing["price"]
            )

    # -----------------------------------------------------
    # SSL
    # -----------------------------------------------------

    ssl = []

    for swing in swing_lows[-10:]:

        if swing["price"] < current_price:

            ssl.append(
                swing["price"]
            )

    # -----------------------------------------------------
    # EQH / EQL
    # -----------------------------------------------------

    eqh = find_equal_highs(
        swing_highs
    )

    eql = find_equal_lows(
        swing_lows
    )

    # -----------------------------------------------------
    # PDH / PDL
    # -----------------------------------------------------

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

    if response.status_code not in (
        200,
        204
    ):

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

print()
print("🚀 ICT/SMC ENGINE — STRUCTURE + LIQUIDITY")
print("============================================")

print(
    f"📌 Symbol: {SYMBOL}"
)

print(
    "⏱️ Timeframe: 1H"
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
    f"\n🔺 Swing Highs: "
    f"{len(swing_highs)}"
)

print(
    f"🔻 Swing Lows: "
    f"{len(swing_lows)}"
)


# =========================================================
# LABELS
# =========================================================

high_labels, low_labels = label_swings(
    swing_highs,
    swing_lows
)


# =========================================================
# HISTORICAL EVENTS
# =========================================================

events = detect_historical_events(
    df,
    swing_highs,
    swing_lows
)


# =========================================================
# CURRENT STRUCTURE
# =========================================================

structure = get_current_structure(
    swing_highs,
    swing_lows,
    events
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

print("\n============================================")

print(
    f"📊 CURRENT BIAS: "
    f"{structure['bias']}"
)

print("\n🏗️ CURRENT STRUCTURE")

if structure["last_high"]:

    print(
        f"🔺 High: "
        f"{structure['last_high']['label']} "
        f"@ "
        f"${structure['last_high']['price']:.4f}"
    )

if structure["last_low"]:

    print(
        f"🔻 Low: "
        f"{structure['last_low']['label']} "
        f"@ "
        f"${structure['last_low']['price']:.4f}"
    )


# =========================================================
# HISTORICAL EVENTS OUTPUT
# =========================================================

print("\n🚨 HISTORICAL STRUCTURE EVENTS")

if events:

    for event in events[-5:]:

        print(
            f"{event['time']} | "
            f"{event['event']} | "
            f"{event['direction']} | "
            f"${event['price']:.4f}"
        )

else:

    print(
        "No confirmed BOS / CHOCH events found."
    )


# =========================================================
# LAST EVENT
# =========================================================

print("\n🎯 LAST CONFIRMED EVENT")

if events:

    last_event = events[-1]

    print(
        f"Event: "
        f"{last_event['event']}"
    )

    print(
        f"Direction: "
        f"{last_event['direction']}"
    )

    print(
        f"Broken Level: "
        f"${last_event['price']:.4f}"
    )

    print(
        f"Level Type: "
        f"{last_event['level_type']}"
    )

    print(
        f"Break Time: "
        f"{last_event['time']}"
    )

else:

    print(
        "None"
    )


# =========================================================
# LIQUIDITY OUTPUT
# =========================================================

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

    print(
        "\n🔺 EQH: None"
    )


if liquidity["eql"]:

    print("\n🔻 EQL")

    for price in liquidity["eql"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🔻 EQL: None"
    )


if liquidity["bsl"]:

    print("\n🟢 BSL")

    for price in liquidity["bsl"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🟢 BSL: None"
    )


if liquidity["ssl"]:

    print("\n🔴 SSL")

    for price in liquidity["ssl"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🔴 SSL: None"
    )


print("\n📅 PREVIOUS DAY LEVELS")

if liquidity["pdh"] is not None:

    print(
        f"PDH: "
        f"${liquidity['pdh']:.4f}"
    )

else:

    print(
        "PDH: None"
    )


if liquidity["pdl"] is not None:

    print(
        f"PDL: "
        f"${liquidity['pdl']:.4f}"
    )

else:

    print(
        "PDL: None"
    )


print("\n============================================")


# =========================================================
# DISCORD MESSAGE
# =========================================================

message = (
    "🧠 **LTCUSDT.P — ICT/SMC ENGINE**\n\n"

    f"💰 Current Price: "
    f"`${liquidity['current_price']:.4f}`\n"

    "⏱️ Timeframe: `1H`\n\n"

    "🏗️ **CURRENT STRUCTURE**\n"
    f"📊 Bias: **{structure['bias']}**\n"
)


# Current High

if structure["last_high"]:

    message += (
        f"🔺 High: "
        f"**{structure['last_high']['label']}** "
        f"@ "
        f"`${structure['last_high']['price']:.4f}`\n"
    )


# Current Low

if structure["last_low"]:

    message += (
        f"🔻 Low: "
        f"**{structure['last_low']['label']}** "
        f"@ "
        f"`${structure['last_low']['price']:.4f}`\n"
    )


# =========================================================
# LAST CONFIRMED EVENT
# =========================================================

message += "\n🎯 **LAST CONFIRMED EVENT**\n"

if events:

    last_event = events[-1]

    message += (
        f"Event: **{last_event['event']}**\n"
        f"Direction: **{last_event['direction']}**\n"
        f"Broken Level: "
        f"`${last_event['price']:.4f}`\n"
        f"Level: "
        f"`{last_event['level_type']}`\n"
        f"Time: "
        f"`{last_event['time']}`\n"
    )

else:

    message += (
        "Event: `None`\n"
    )


# =========================================================
# RECENT EVENTS
# =========================================================

message += "\n📜 **RECENT STRUCTURE HISTORY**\n"

if events:

    for event in events[-3:]:

        message += (
            f"• `{event['time']}` — "
            f"**{event['event']} "
            f"{event['direction']}** "
            f"@ `${event['price']:.4f}`\n"
        )

else:

    message += (
        "• No confirmed BOS / CHOCH\n"
    )


# =========================================================
# LIQUIDITY
# =========================================================

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


# =========================================================
# PDH / PDL
# =========================================================

message += "\n📅 **PREVIOUS DAY LEVELS**\n"

if liquidity["pdh"] is not None:

    message += (
        f"PDH: `${liquidity['pdh']:.4f}`\n"
    )

else:

    message += (
        "PDH: `None`\n"
    )


if liquidity["pdl"] is not None:

    message += (
        f"PDL: `${liquidity['pdl']:.4f}`\n"
    )

else:

    message += (
        "PDL: `None`\n"
    )


message += (
    "\n🤖 **ICT/SMC Engine**\n"
    "✅ Current Structure + Historical Events "
    "+ Liquidity Scan Complete"
)


# =========================================================
# SEND DISCORD
# =========================================================

print(
    "\n📡 Sending upgraded analysis to Discord..."
)

send_discord_message(
    message
)


print(
    "\n✅ UPGRADED ICT/SMC ENGINE TEST COMPLETE!"
)
