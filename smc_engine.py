import requests
import pandas as pd
import os


# =========================================================
# SETTINGS
# =========================================================

SYMBOL = "BTC_USDT"

TIMEFRAME = "Min60"

CANDLE_LIMIT = 150

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

SWING_STRENGTH = 2

# Equal High / Equal Low tolerance
# 0.001 = 0.1%
EQUAL_TOLERANCE = 0.001

# Number of recent closed candles used
# to build the MAJOR dealing range
MAJOR_RANGE_CANDLES = 100

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


# =========================================================
# DISPLAY SYMBOL
# =========================================================

DISPLAY_SYMBOL = (
    SYMBOL.replace("_USDT", "USDT") + ".P"
)


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

    df = df.tail(limit)

    df = df.reset_index(
        drop=True
    )

    return df


# =========================================================
# FIND CONFIRMED SWINGS
# =========================================================

def find_swings(
    df,
    strength=2
):

    swing_highs = []

    swing_lows = []

    for i in range(
        strength,
        len(df) - strength
    ):

        current_high = df.loc[
            i,
            "high"
        ]

        current_low = df.loc[
            i,
            "low"
        ]

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

        # -----------------------------------------
        # Swing High
        # -----------------------------------------

        if (
            current_high > left_highs.max()
            and
            current_high > right_highs.max()
        ):

            swing_highs.append({

                "index": i,

                "price": float(
                    current_high
                ),

                "time": df.loc[
                    i,
                    "time"
                ]

            })

        # -----------------------------------------
        # Swing Low
        # -----------------------------------------

        if (
            current_low < left_lows.min()
            and
            current_low < right_lows.min()
        ):

            swing_lows.append({

                "index": i,

                "price": float(
                    current_low
                ),

                "time": df.loc[
                    i,
                    "time"
                ]

            })

    return (
        swing_highs,
        swing_lows
    )


# =========================================================
# LABEL HH / HL / LH / LL
# =========================================================

def label_swings(
    swing_highs,
    swing_lows
):

    high_labels = []

    low_labels = []

    # -----------------------------------------
    # Highs
    # -----------------------------------------

    for i in range(
        1,
        len(swing_highs)
    ):

        current = swing_highs[i]

        previous = swing_highs[i - 1]

        if (
            current["price"]
            >
            previous["price"]
        ):

            label = "HH"

        elif (
            current["price"]
            <
            previous["price"]
        ):

            label = "LH"

        else:

            label = "EQH"

        high_labels.append({

            "label": label,

            "price": current["price"],

            "time": current["time"],

            "index": current["index"]

        })

    # -----------------------------------------
    # Lows
    # -----------------------------------------

    for i in range(
        1,
        len(swing_lows)
    ):

        current = swing_lows[i]

        previous = swing_lows[i - 1]

        if (
            current["price"]
            >
            previous["price"]
        ):

            label = "HL"

        elif (
            current["price"]
            <
            previous["price"]
        ):

            label = "LL"

        else:

            label = "EQL"

        low_labels.append({

            "label": label,

            "price": current["price"],

            "time": current["time"],

            "index": current["index"]

        })

    return (
        high_labels,
        low_labels
    )


# =========================================================
# INITIAL STRUCTURAL BIAS
# =========================================================

def determine_initial_bias(
    swing_highs,
    swing_lows
):

    if (
        len(swing_highs) < 2
        or
        len(swing_lows) < 2
    ):

        return "UNKNOWN"

    last_high = swing_highs[-1]["price"]

    previous_high = swing_highs[-2]["price"]

    last_low = swing_lows[-1]["price"]

    previous_low = swing_lows[-2]["price"]

    higher_high = (
        last_high
        >
        previous_high
    )

    higher_low = (
        last_low
        >
        previous_low
    )

    lower_high = (
        last_high
        <
        previous_high
    )

    lower_low = (
        last_low
        <
        previous_low
    )

    if (
        higher_high
        and
        higher_low
    ):

        return "BULLISH"

    if (
        lower_high
        and
        lower_low
    ):

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

    closed_df = df.iloc[:-1].copy()

    if closed_df.empty:

        return events

    bias = determine_initial_bias(
        swing_highs,
        swing_lows
    )

    broken_high_indices = set()

    broken_low_indices = set()

    for candle_index in range(
        len(closed_df)
    ):

        candle = closed_df.iloc[
            candle_index
        ]

        close_price = float(
            candle["close"]
        )

        candle_time = candle["time"]

        # -----------------------------------------
        # Confirmed highs available before candle
        # -----------------------------------------

        available_highs = [

            swing

            for swing in swing_highs

            if swing["index"]
            <
            candle_index

        ]

        # -----------------------------------------
        # Confirmed lows available before candle
        # -----------------------------------------

        available_lows = [

            swing

            for swing in swing_lows

            if swing["index"]
            <
            candle_index

        ]

        # =================================================
        # BULLISH BREAK
        # =================================================

        if available_highs:

            latest_high = available_highs[-1]

            high_index = latest_high[
                "index"
            ]

            high_price = latest_high[
                "price"
            ]

            if (
                high_index
                not in
                broken_high_indices

                and

                close_price
                >
                high_price
            ):

                if bias == "BEARISH":

                    event_type = "CHOCH"

                else:

                    event_type = "BOS"

                events.append({

                    "event": event_type,

                    "direction": "BULLISH",

                    "price": high_price,

                    "time": candle_time,

                    "level_type":
                        "SWING HIGH",

                    "candle_close":
                        close_price

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

            low_index = latest_low[
                "index"
            ]

            low_price = latest_low[
                "price"
            ]

            if (
                low_index
                not in
                broken_low_indices

                and

                close_price
                <
                low_price
            ):

                if bias == "BULLISH":

                    event_type = "CHOCH"

                else:

                    event_type = "BOS"

                events.append({

                    "event": event_type,

                    "direction": "BEARISH",

                    "price": low_price,

                    "time": candle_time,

                    "level_type":
                        "SWING LOW",

                    "candle_close":
                        close_price

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

    # -----------------------------------------
    # Current bias
    # -----------------------------------------

    if events:

        latest_event = events[-1]

        if (
            latest_event["direction"]
            ==
            "BULLISH"
        ):

            bias = "BULLISH"

        elif (
            latest_event["direction"]
            ==
            "BEARISH"
        ):

            bias = "BEARISH"

        else:

            bias = "UNKNOWN"

    else:

        bias = determine_initial_bias(
            swing_highs,
            swing_lows
        )

    # -----------------------------------------
    # Latest high structure
    # -----------------------------------------

    last_high = None

    if len(swing_highs) >= 2:

        previous = swing_highs[
            -2
        ]["price"]

        current = swing_highs[
            -1
        ]["price"]

        if current > previous:

            label = "HH"

        elif current < previous:

            label = "LH"

        else:

            label = "EQH"

        last_high = {

            "label": label,

            "price": current,

            "time":
                swing_highs[-1]["time"]

        }

    # -----------------------------------------
    # Latest low structure
    # -----------------------------------------

    last_low = None

    if len(swing_lows) >= 2:

        previous = swing_lows[
            -2
        ]["price"]

        current = swing_lows[
            -1
        ]["price"]

        if current > previous:

            label = "HL"

        elif current < previous:

            label = "LL"

        else:

            label = "EQL"

        last_low = {

            "label": label,

            "price": current,

            "time":
                swing_lows[-1]["time"]

        }

    return {

        "bias": bias,

        "last_high": last_high,

        "last_low": last_low

    }


# =========================================================
# EQUAL PRICE CHECK
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
        <=
        EQUAL_TOLERANCE
    )


# =========================================================
# EQH
# =========================================================

def find_equal_highs(
    swing_highs
):

    levels = []

    if len(swing_highs) < 2:

        return levels

    for i in range(
        len(swing_highs) - 1
    ):

        first = swing_highs[i]

        second = swing_highs[
            i + 1
        ]

        if prices_are_equal(
            first["price"],
            second["price"]
        ):

            level = (

                first["price"]

                +

                second["price"]

            ) / 2

            levels.append(
                level
            )

    return levels


# =========================================================
# EQL
# =========================================================

def find_equal_lows(
    swing_lows
):

    levels = []

    if len(swing_lows) < 2:

        return levels

    for i in range(
        len(swing_lows) - 1
    ):

        first = swing_lows[i]

        second = swing_lows[
            i + 1
        ]

        if prices_are_equal(
            first["price"],
            second["price"]
        ):

            level = (

                first["price"]

                +

                second["price"]

            ) / 2

            levels.append(
                level
            )

    return levels


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
    # Buy-side liquidity
    # -----------------------------------------

    bsl = []

    for swing in swing_highs[-10:]:

        if (
            swing["price"]
            >
            current_price
        ):

            bsl.append(
                swing["price"]
            )

    # -----------------------------------------
    # Sell-side liquidity
    # -----------------------------------------

    ssl = []

    for swing in swing_lows[-10:]:

        if (
            swing["price"]
            <
            current_price
        ):

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

    dates = temp_df[
        "date"
    ].unique()

    pdh = None

    pdl = None

    if len(dates) >= 2:

        previous_date = dates[-2]

        previous_day = temp_df[
            temp_df["date"]
            ==
            previous_date
        ]

        if not previous_day.empty:

            pdh = float(
                previous_day["high"]
                .max()
            )

            pdl = float(
                previous_day["low"]
                .min()
            )

    return {

        "current_price":
            current_price,

        "bsl": bsl,

        "ssl": ssl,

        "eqh": eqh,

        "eql": eql,

        "pdh": pdh,

        "pdl": pdl

    }


# =========================================================
# MAJOR DEALING RANGE
# =========================================================

def analyze_major_range(
    df,
    swing_highs,
    swing_lows
):

    # -----------------------------------------
    # Use CLOSED candles only
    # -----------------------------------------

    closed_df = df.iloc[:-1].copy()

    if closed_df.empty:

        return {

            "status":
                "NOT ENOUGH DATA",

            "high": None,

            "low": None,

            "equilibrium": None,

            "position_percent": None,

            "zone": "UNKNOWN"

        }

    lookback = min(
        MAJOR_RANGE_CANDLES,
        len(closed_df)
    )

    range_start_index = (
        len(closed_df)
        -
        lookback
    )

    range_df = closed_df.iloc[
        range_start_index:
    ]

    # -----------------------------------------
    # Only confirmed swings inside range
    # -----------------------------------------

    valid_highs = [

        swing

        for swing in swing_highs

        if swing["index"]
        <
        len(closed_df)

        and

        swing["index"]
        >=
        range_start_index

    ]

    valid_lows = [

        swing

        for swing in swing_lows

        if swing["index"]
        <
        len(closed_df)

        and

        swing["index"]
        >=
        range_start_index

    ]

    # -----------------------------------------
    # Prefer confirmed swing extremes
    # -----------------------------------------

    if valid_highs:

        range_high = max(
            swing["price"]
            for swing in valid_highs
        )

    else:

        range_high = float(
            range_df["high"].max()
        )

    if valid_lows:

        range_low = min(
            swing["price"]
            for swing in valid_lows
        )

    else:

        range_low = float(
            range_df["low"].min()
        )

    # -----------------------------------------
    # Equilibrium
    # -----------------------------------------

    equilibrium = (
        range_high
        +
        range_low
    ) / 2

    range_size = (
        range_high
        -
        range_low
    )

    current_price = float(
        df["close"].iloc[-1]
    )

    if range_size <= 0:

        return {

            "status":
                "INVALID RANGE",

            "high":
                range_high,

            "low":
                range_low,

            "equilibrium":
                equilibrium,

            "position_percent":
                None,

            "zone":
                "UNKNOWN"

        }

    position_percent = (

        (
            current_price
            -
            range_low
        )

        /

        range_size

    ) * 100

    if (
        current_price
        >
        equilibrium
    ):

        zone = "PREMIUM"

    elif (
        current_price
        <
        equilibrium
    ):

        zone = "DISCOUNT"

    else:

        zone = "EQUILIBRIUM"

    return {

        "status": "OK",

        "high":
            range_high,

        "low":
            range_low,

        "equilibrium":
            equilibrium,

        "position_percent":
            position_percent,

        "zone":
            zone

    }


# =========================================================
# LOCAL DEALING RANGE
# =========================================================

def analyze_local_range(
    df,
    swing_highs,
    swing_lows
):

    current_price = float(
        df["close"].iloc[-1]
    )

    if (
        not swing_highs
        or
        not swing_lows
    ):

        return {

            "status":
                "NOT ENOUGH DATA",

            "high": None,

            "low": None,

            "equilibrium": None,

            "position_percent": None,

            "zone": "UNKNOWN"

        }

    # -----------------------------------------
    # Latest confirmed swing high / low
    # -----------------------------------------

    range_high = float(
        swing_highs[-1]["price"]
    )

    range_low = float(
        swing_lows[-1]["price"]
    )

    if range_high <= range_low:

        return {

            "status":
                "INVALID RANGE",

            "high":
                range_high,

            "low":
                range_low,

            "equilibrium":
                None,

            "position_percent":
                None,

            "zone":
                "UNKNOWN"

        }

    equilibrium = (
        range_high
        +
        range_low
    ) / 2

    range_size = (
        range_high
        -
        range_low
    )

    position_percent = (

        (
            current_price
            -
            range_low
        )

        /

        range_size

    ) * 100

    if (
        current_price
        >
        equilibrium
    ):

        zone = "PREMIUM"

    elif (
        current_price
        <
        equilibrium
    ):

        zone = "DISCOUNT"

    else:

        zone = "EQUILIBRIUM"

    return {

        "status":
            "OK",

        "high":
            range_high,

        "low":
            range_low,

        "equilibrium":
            equilibrium,

        "position_percent":
            position_percent,

        "zone":
            zone

    }


# =========================================================
# PREMIUM / DISCOUNT ALIGNMENT
# =========================================================

def determine_alignment(
    bias,
    major_zone,
    local_zone
):

    # -----------------------------------------
    # Bullish
    # -----------------------------------------

    if bias == "BULLISH":

        if (
            major_zone
            ==
            "DISCOUNT"
            and
            local_zone
            ==
            "DISCOUNT"
        ):

            return "ALIGNED"

        if (
            major_zone
            ==
            "PREMIUM"
            and
            local_zone
            ==
            "PREMIUM"
        ):

            return "NOT ALIGNED"

        return "MIXED"

    # -----------------------------------------
    # Bearish
    # -----------------------------------------

    if bias == "BEARISH":

        if (
            major_zone
            ==
            "PREMIUM"
            and
            local_zone
            ==
            "PREMIUM"
        ):

            return "ALIGNED"

        if (
            major_zone
            ==
            "DISCOUNT"
            and
            local_zone
            ==
            "DISCOUNT"
        ):

            return "NOT ALIGNED"

        return "MIXED"

    return "NO CLEAR BIAS"


# =========================================================
# PREMIUM / DISCOUNT ENGINE
# =========================================================

def analyze_premium_discount(
    df,
    swing_highs,
    swing_lows,
    bias
):

    major = analyze_major_range(
        df,
        swing_highs,
        swing_lows
    )

    local = analyze_local_range(
        df,
        swing_highs,
        swing_lows
    )

    if (
        major["status"]
        !=
        "OK"
        or
        local["status"]
        !=
        "OK"
    ):

        alignment = "UNKNOWN"

    else:

        alignment = determine_alignment(
            bias,
            major["zone"],
            local["zone"]
        )

    return {

        "major": major,

        "local": local,

        "alignment":
            alignment

    }


# =========================================================
# DISCORD
# =========================================================

def send_discord_message(
    message
):

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

        print(
            response.text
        )

        response.raise_for_status()

    print(
        "✅ Discord message sent successfully!"
    )


# =========================================================
# MAIN
# =========================================================

print()

print(
    "🚀 ICT/SMC ENGINE — STEP 3 UPGRADE"
)

print(
    "============================================"
)

print(
    f"📌 Symbol: {DISPLAY_SYMBOL}"
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
# STRUCTURE LABELS
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
# PREMIUM / DISCOUNT
# =========================================================

premium_discount = analyze_premium_discount(

    df,

    swing_highs,

    swing_lows,

    structure["bias"]

)

major = premium_discount[
    "major"
]

local = premium_discount[
    "local"
]

alignment = premium_discount[
    "alignment"
]


# =========================================================
# TERMINAL — STRUCTURE
# =========================================================

print()

print(
    "============================================"
)

print(
    f"📊 CURRENT BIAS: "
    f"{structure['bias']}"
)

print()

print(
    "🏗️ CURRENT STRUCTURE"
)

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
# HISTORICAL EVENTS
# =========================================================

print()

print(
    "🚨 HISTORICAL STRUCTURE EVENTS"
)

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

print()

print(
    "🎯 LAST CONFIRMED EVENT"
)

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
        f"Level: "
        f"{last_event['level_type']}"
    )

    print(
        f"Time: "
        f"{last_event['time']}"
    )

else:

    print(
        "None"
    )


# =========================================================
# LIQUIDITY
# =========================================================

print()

print(
    "💧 LIQUIDITY"
)

print(
    f"💰 Current Price: "
    f"${liquidity['current_price']:.4f}"
)


if liquidity["eqh"]:

    print(
        "\n🔺 EQH"
    )

    for price in liquidity["eqh"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🔺 EQH: None"
    )


if liquidity["eql"]:

    print(
        "\n🔻 EQL"
    )

    for price in liquidity["eql"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🔻 EQL: None"
    )


if liquidity["bsl"]:

    print(
        "\n🟢 BSL"
    )

    for price in liquidity["bsl"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🟢 BSL: None"
    )


if liquidity["ssl"]:

    print(
        "\n🔴 SSL"
    )

    for price in liquidity["ssl"][-5:]:

        print(
            f"   ${price:.4f}"
        )

else:

    print(
        "\n🔴 SSL: None"
    )


print()

print(
    "📅 PREVIOUS DAY LEVELS"
)

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


# =========================================================
# MAJOR RANGE OUTPUT
# =========================================================

print()

print(
    "📐 MAJOR DEALING RANGE"
)

if major["status"] == "OK":

    print(

        f"🔺 Range High: "
        f"${major['high']:.4f}"

    )

    print(

        f"🔻 Range Low: "
        f"${major['low']:.4f}"

    )

    print(

        f"⚖️ 50% Equilibrium: "
        f"${major['equilibrium']:.4f}"

    )

    print(

        f"📊 Position: "
        f"{major['position_percent']:.2f}%"

    )

    print(

        f"📍 Zone: "
        f"{major['zone']}"

    )

else:

    print(
        f"Status: {major['status']}"
    )


# =========================================================
# LOCAL RANGE OUTPUT
# =========================================================

print()

print(
    "📏 LOCAL DEALING RANGE"
)

if local["status"] == "OK":

    print(

        f"🔺 Range High: "
        f"${local['high']:.4f}"

    )

    print(

        f"🔻 Range Low: "
        f"${local['low']:.4f}"

    )

    print(

        f"⚖️ 50% Equilibrium: "
        f"${local['equilibrium']:.4f}"

    )

    print(

        f"📊 Position: "
        f"{local['position_percent']:.2f}%"

    )

    print(

        f"📍 Zone: "
        f"{local['zone']}"

    )

else:

    print(
        f"Status: {local['status']}"
    )


# =========================================================
# ALIGNMENT
# =========================================================

print()

print(
    "🎯 PREMIUM / DISCOUNT ALIGNMENT"
)

print(
    f"Bias: {structure['bias']}"
)

print(
    f"Major Zone: {major['zone']}"
)

print(
    f"Local Zone: {local['zone']}"
)

print(
    f"Alignment: {alignment}"
)

print()

print(
    "============================================"
)


# =========================================================
# DISCORD MESSAGE
# =========================================================

message = (

    f"🧠 **{DISPLAY_SYMBOL} — ICT/SMC ENGINE**\n\n"

    f"💰 Current Price: "
    f"`${liquidity['current_price']:.4f}`\n"

    "⏱️ Timeframe: `1H`\n\n"

)


# =========================================================
# CURRENT STRUCTURE
# =========================================================

message += (
    "🏗️ **CURRENT STRUCTURE**\n"
)

message += (
    f"📊 Bias: "
    f"**{structure['bias']}**\n"
)


if structure["last_high"]:

    message += (

        f"🔺 High: "
        f"**{structure['last_high']['label']}** "
        f"@ "
        f"`${structure['last_high']['price']:.4f}`\n"

    )


if structure["last_low"]:

    message += (

        f"🔻 Low: "
        f"**{structure['last_low']['label']}** "
        f"@ "
        f"`${structure['last_low']['price']:.4f}`\n"

    )


# =========================================================
# LAST EVENT
# =========================================================

message += (
    "\n🎯 **LAST CONFIRMED EVENT**\n"
)

if events:

    last_event = events[-1]

    message += (

        f"Event: "
        f"**{last_event['event']}**\n"

        f"Direction: "
        f"**{last_event['direction']}**\n"

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
# RECENT HISTORY
# =========================================================

message += (
    "\n📜 **RECENT STRUCTURE HISTORY**\n"
)

if events:

    for event in events[-3:]:

        message += (

            f"• `{event['time']}` — "
            f"**{event['event']} "
            f"{event['direction']}** "
            f"@ "
            f"`${event['price']:.4f}`\n"

        )

else:

    message += (
        "• No confirmed BOS / CHOCH\n"
    )


# =========================================================
# LIQUIDITY
# =========================================================

message += (
    "\n💧 **LIQUIDITY**\n"
)


if liquidity["eqh"]:

    message += (
        "\n🔺 **EQH**\n"
    )

    for price in liquidity["eqh"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🔺 EQH: `None`\n"
    )


if liquidity["eql"]:

    message += (
        "\n🔻 **EQL**\n"
    )

    for price in liquidity["eql"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🔻 EQL: `None`\n"
    )


if liquidity["bsl"]:

    message += (
        "\n🟢 **BSL**\n"
    )

    for price in liquidity["bsl"][-3:]:

        message += (
            f"• `${price:.4f}`\n"
        )

else:

    message += (
        "🟢 BSL: `None`\n"
    )


if liquidity["ssl"]:

    message += (
        "\n🔴 **SSL**\n"
    )

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

message += (
    "\n📅 **PREVIOUS DAY LEVELS**\n"
)


if liquidity["pdh"] is not None:

    message += (
        f"PDH: "
        f"`${liquidity['pdh']:.4f}`\n"
    )

else:

    message += (
        "PDH: `None`\n"
    )


if liquidity["pdl"] is not None:

    message += (
        f"PDL: "
        f"`${liquidity['pdl']:.4f}`\n"
    )

else:

    message += (
        "PDL: `None`\n"
    )


# =========================================================
# MAJOR DEALING RANGE
# =========================================================

message += (
    "\n📐 **MAJOR DEALING RANGE**\n"
)


if major["status"] == "OK":

    message += (

        f"🔺 High: "
        f"`${major['high']:.4f}`\n"

        f"🔻 Low: "
        f"`${major['low']:.4f}`\n"

        f"⚖️ 50% Equilibrium: "
        f"`${major['equilibrium']:.4f}`\n"

        f"📊 Position: "
        f"`{major['position_percent']:.2f}%`\n"

        f"📍 Zone: "
        f"**{major['zone']}**\n"

    )

else:

    message += (
        f"Status: `{major['status']}`\n"
    )


# =========================================================
# LOCAL DEALING RANGE
# =========================================================

message += (
    "\n📏 **LOCAL DEALING RANGE**\n"
)


if local["status"] == "OK":

    message += (

        f"🔺 High: "
        f"`${local['high']:.4f}`\n"

        f"🔻 Low: "
        f"`${local['low']:.4f}`\n"

        f"⚖️ 50% Equilibrium: "
        f"`${local['equilibrium']:.4f}`\n"

        f"📊 Position: "
        f"`{local['position_percent']:.2f}%`\n"

        f"📍 Zone: "
        f"**{local['zone']}**\n"

    )

else:

    message += (
        f"Status: `{local['status']}`\n"
    )


# =========================================================
# ALIGNMENT
# =========================================================

message += (
    "\n🎯 **PREMIUM / DISCOUNT ALIGNMENT**\n"

    f"Bias: **{structure['bias']}**\n"

    f"Major Zone: **{major['zone']}**\n"

    f"Local Zone: **{local['zone']}**\n"

    f"Alignment: **{alignment}**\n"
)


# =========================================================
# FINAL STATUS
# =========================================================

message += (

    "\n🤖 **ICT/SMC Engine**\n"

    "✅ Structure\n"

    "✅ Historical BOS/CHOCH\n"

    "✅ Liquidity\n"

    "✅ Major + Local Premium/Discount"

)


# =========================================================
# SEND DISCORD
# =========================================================

print()

print(
    "📡 Sending Step 3 upgrade to Discord..."
)

send_discord_message(
    message
)


print()

print(
    "✅ STEP 3 UPGRADE TEST COMPLETE!"
)
