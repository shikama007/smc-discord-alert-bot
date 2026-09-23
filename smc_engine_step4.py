import requests
import pandas as pd
import os

# =========================================================
# SETTINGS
# =========================================================

SYMBOL = "LTC_USDT"
TIMEFRAME = "Min60"
CANDLE_LIMIT = 200

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

SWING_STRENGTH = 2
EQUAL_TOLERANCE = 0.001
MAJOR_RANGE_CANDLES = 100

# POI settings
FVG_MIN_SIZE_PCT = 0.05       # minimum FVG size as % of price
POI_LOOKBACK = 80             # candles to scan for POIs
POI_NEAR_PCT = 0.50           # current price considered "near" POI within 0.50%

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

DISPLAY_SYMBOL = SYMBOL.replace("_USDT", "USDT") + ".P"


# =========================================================
# DATA
# =========================================================

def get_candles(symbol, interval, limit=200):
    url = f"{BASE_URL}/{symbol}"
    response = requests.get(
        url,
        params={"interval": interval},
        timeout=10
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("success"):
        raise RuntimeError(f"MEXC API Error: {result}")

    data = result["data"]

    df = pd.DataFrame({
        "time": data["time"],
        "open": data["open"],
        "high": data["high"],
        "low": data["low"],
        "close": data["close"],
        "volume": data["vol"]
    })

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.dropna().tail(limit).reset_index(drop=True)

    # IMPORTANT: use only fully closed candles for analysis.
    # The final row can still be forming.
    return df


# =========================================================
# SWINGS
# =========================================================

def find_swings(df, strength=2):
    swing_highs = []
    swing_lows = []

    for i in range(strength, len(df) - strength):
        current_high = df.loc[i, "high"]
        current_low = df.loc[i, "low"]

        left_highs = df.loc[i-strength:i-1, "high"]
        right_highs = df.loc[i+1:i+strength, "high"]

        left_lows = df.loc[i-strength:i-1, "low"]
        right_lows = df.loc[i+1:i+strength, "low"]

        if current_high > left_highs.max() and current_high > right_highs.max():
            swing_highs.append({
                "index": i,
                "price": float(current_high),
                "time": df.loc[i, "time"]
            })

        if current_low < left_lows.min() and current_low < right_lows.min():
            swing_lows.append({
                "index": i,
                "price": float(current_low),
                "time": df.loc[i, "time"]
            })

    return swing_highs, swing_lows


# =========================================================
# STRUCTURE
# =========================================================

def determine_initial_bias(swing_highs, swing_lows):
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "UNKNOWN"

    hh = swing_highs[-1]["price"] > swing_highs[-2]["price"]
    hl = swing_lows[-1]["price"] > swing_lows[-2]["price"]
    lh = swing_highs[-1]["price"] < swing_highs[-2]["price"]
    ll = swing_lows[-1]["price"] < swing_lows[-2]["price"]

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "RANGE"


def detect_historical_events(df, swing_highs, swing_lows):
    events = []
    closed_df = df.iloc[:-1].copy()

    if closed_df.empty:
        return events

    bias = determine_initial_bias(swing_highs, swing_lows)
    broken_high_indices = set()
    broken_low_indices = set()

    for candle_index in range(len(closed_df)):
        candle = closed_df.iloc[candle_index]
        close_price = float(candle["close"])
        candle_time = candle["time"]

        available_highs = [
            s for s in swing_highs
            if s["index"] < candle_index
        ]
        available_lows = [
            s for s in swing_lows
            if s["index"] < candle_index
        ]

        if available_highs:
            latest_high = available_highs[-1]
            hi_idx = latest_high["index"]
            hi_price = latest_high["price"]

            if hi_idx not in broken_high_indices and close_price > hi_price:
                event_type = "CHOCH" if bias == "BEARISH" else "BOS"

                events.append({
                    "event": event_type,
                    "direction": "BULLISH",
                    "price": hi_price,
                    "time": candle_time,
                    "level_type": "SWING HIGH",
                    "candle_close": close_price
                })

                broken_high_indices.add(hi_idx)
                bias = "BULLISH"

        if available_lows:
            latest_low = available_lows[-1]
            low_idx = latest_low["index"]
            low_price = latest_low["price"]

            if low_idx not in broken_low_indices and close_price < low_price:
                event_type = "CHOCH" if bias == "BULLISH" else "BOS"

                events.append({
                    "event": event_type,
                    "direction": "BEARISH",
                    "price": low_price,
                    "time": candle_time,
                    "level_type": "SWING LOW",
                    "candle_close": close_price
                })

                broken_low_indices.add(low_idx)
                bias = "BEARISH"

    return events


def get_current_structure(swing_highs, swing_lows, events):
    if events:
        bias = events[-1]["direction"]
    else:
        bias = determine_initial_bias(swing_highs, swing_lows)

    last_high = None
    last_low = None

    if len(swing_highs) >= 2:
        current = swing_highs[-1]["price"]
        previous = swing_highs[-2]["price"]
        label = "HH" if current > previous else "LH" if current < previous else "EQH"
        last_high = {
            "label": label,
            "price": current,
            "time": swing_highs[-1]["time"]
        }

    if len(swing_lows) >= 2:
        current = swing_lows[-1]["price"]
        previous = swing_lows[-2]["price"]
        label = "HL" if current > previous else "LL" if current < previous else "EQL"
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
# LIQUIDITY
# =========================================================

def prices_are_equal(a, b):
    avg = (a + b) / 2
    if avg == 0:
        return False
    return abs(a - b) / avg <= EQUAL_TOLERANCE


def find_equal_highs(swing_highs):
    levels = []
    for i in range(len(swing_highs) - 1):
        a = swing_highs[i]["price"]
        b = swing_highs[i + 1]["price"]
        if prices_are_equal(a, b):
            levels.append((a + b) / 2)
    return levels


def find_equal_lows(swing_lows):
    levels = []
    for i in range(len(swing_lows) - 1):
        a = swing_lows[i]["price"]
        b = swing_lows[i + 1]["price"]
        if prices_are_equal(a, b):
            levels.append((a + b) / 2)
    return levels


def analyze_liquidity(df, swing_highs, swing_lows):
    current_price = float(df["close"].iloc[-1])

    bsl = [s["price"] for s in swing_highs[-10:] if s["price"] > current_price]
    ssl = [s["price"] for s in swing_lows[-10:] if s["price"] < current_price]

    temp = df.copy()
    temp["date"] = temp["time"].dt.date
    dates = temp["date"].unique()

    pdh = pdl = None
    if len(dates) >= 2:
        previous_day = temp[temp["date"] == dates[-2]]
        if not previous_day.empty:
            pdh = float(previous_day["high"].max())
            pdl = float(previous_day["low"].min())

    return {
        "current_price": current_price,
        "bsl": bsl,
        "ssl": ssl,
        "eqh": find_equal_highs(swing_highs),
        "eql": find_equal_lows(swing_lows),
        "pdh": pdh,
        "pdl": pdl
    }


# =========================================================
# PREMIUM / DISCOUNT
# =========================================================

def analyze_major_range(df, swing_highs, swing_lows):
    closed_df = df.iloc[:-1].copy()
    lookback = min(MAJOR_RANGE_CANDLES, len(closed_df))
    start = len(closed_df) - lookback

    valid_highs = [
        s for s in swing_highs
        if start <= s["index"] < len(closed_df)
    ]
    valid_lows = [
        s for s in swing_lows
        if start <= s["index"] < len(closed_df)
    ]

    range_high = (
        max(s["price"] for s in valid_highs)
        if valid_highs else float(closed_df["high"].max())
    )
    range_low = (
        min(s["price"] for s in valid_lows)
        if valid_lows else float(closed_df["low"].min())
    )

    equilibrium = (range_high + range_low) / 2
    current = float(df["close"].iloc[-1])
    size = range_high - range_low

    if size <= 0:
        return {"status": "INVALID RANGE", "high": range_high, "low": range_low}

    position = ((current - range_low) / size) * 100
    zone = "PREMIUM" if current > equilibrium else "DISCOUNT" if current < equilibrium else "EQUILIBRIUM"

    return {
        "status": "OK",
        "high": range_high,
        "low": range_low,
        "equilibrium": equilibrium,
        "position_percent": position,
        "zone": zone
    }


def analyze_local_range(df, swing_highs, swing_lows):
    current = float(df["close"].iloc[-1])

    if not swing_highs or not swing_lows:
        return {"status": "NOT ENOUGH DATA"}

    high = float(swing_highs[-1]["price"])
    low = float(swing_lows[-1]["price"])

    if high <= low:
        return {"status": "INVALID RANGE"}

    equilibrium = (high + low) / 2
    position = ((current - low) / (high - low)) * 100
    zone = "PREMIUM" if current > equilibrium else "DISCOUNT" if current < equilibrium else "EQUILIBRIUM"

    return {
        "status": "OK",
        "high": high,
        "low": low,
        "equilibrium": equilibrium,
        "position_percent": position,
        "zone": zone
    }


def analyze_premium_discount(df, swing_highs, swing_lows, bias):
    major = analyze_major_range(df, swing_highs, swing_lows)
    local = analyze_local_range(df, swing_highs, swing_lows)

    if major.get("status") != "OK" or local.get("status") != "OK":
        alignment = "UNKNOWN"
    elif bias == "BULLISH":
        alignment = (
            "ALIGNED" if major["zone"] == "DISCOUNT" and local["zone"] == "DISCOUNT"
            else "NOT ALIGNED" if major["zone"] == "PREMIUM" and local["zone"] == "PREMIUM"
            else "MIXED"
        )
    elif bias == "BEARISH":
        alignment = (
            "ALIGNED" if major["zone"] == "PREMIUM" and local["zone"] == "PREMIUM"
            else "NOT ALIGNED" if major["zone"] == "DISCOUNT" and local["zone"] == "DISCOUNT"
            else "MIXED"
        )
    else:
        alignment = "NO CLEAR BIAS"

    return {"major": major, "local": local, "alignment": alignment}


# =========================================================
# STEP 4 — FVG
# =========================================================

def detect_fvgs(df):
    """
    Three-candle imbalance:
      Bullish FVG: candle[i].low > candle[i-2].high
      Bearish FVG: candle[i].high < candle[i-2].low

    Only closed candles are used.
    """
    closed = df.iloc[:-1].copy()
    fvgs = []

    start = max(2, len(closed) - POI_LOOKBACK)

    for i in range(start, len(closed)):
        c1 = closed.iloc[i - 2]
        c3 = closed.iloc[i]

        # Bullish FVG
        if c3["low"] > c1["high"]:
            lower = float(c1["high"])
            upper = float(c3["low"])
            size_pct = ((upper - lower) / lower) * 100

            if size_pct >= FVG_MIN_SIZE_PCT:
                fvgs.append({
                    "type": "BULLISH FVG",
                    "direction": "BULLISH",
                    "lower": lower,
                    "upper": upper,
                    "mid": (lower + upper) / 2,
                    "time": c3["time"],
                    "index": i,
                    "size_pct": size_pct
                })

        # Bearish FVG
        if c3["high"] < c1["low"]:
            lower = float(c3["high"])
            upper = float(c1["low"])
            size_pct = ((upper - lower) / lower) * 100

            if size_pct >= FVG_MIN_SIZE_PCT:
                fvgs.append({
                    "type": "BEARISH FVG",
                    "direction": "BEARISH",
                    "lower": lower,
                    "upper": upper,
                    "mid": (lower + upper) / 2,
                    "time": c3["time"],
                    "index": i,
                    "size_pct": size_pct
                })

    return fvgs


# =========================================================
# STEP 4 — ORDER BLOCK HEURISTIC
# =========================================================

def detect_order_blocks(df, events):
    """
    Heuristic OB:
    - For a bullish BOS/CHOCH, find the nearest previous bearish
      candle before the break candle.
    - For a bearish BOS/CHOCH, find the nearest previous bullish
      candle before the break candle.

    This is an automated approximation, not a claim that every
    detected candle is a discretionary ICT order block.
    """
    closed = df.iloc[:-1].copy()
    obs = []

    for event in events[-20:]:
        matches = closed.index[closed["time"] == event["time"]].tolist()
        if not matches:
            continue

        break_idx = matches[0]

        if event["direction"] == "BULLISH":
            candidates = range(break_idx - 1, max(-1, break_idx - 8), -1)
            for j in candidates:
                if closed.loc[j, "close"] < closed.loc[j, "open"]:
                    obs.append({
                        "type": "BULLISH OB",
                        "direction": "BULLISH",
                        "lower": float(closed.loc[j, "low"]),
                        "upper": float(closed.loc[j, "high"]),
                        "mid": float((closed.loc[j, "low"] + closed.loc[j, "high"]) / 2),
                        "time": closed.loc[j, "time"],
                        "index": j,
                        "source_event": event["event"]
                    })
                    break

        elif event["direction"] == "BEARISH":
            candidates = range(break_idx - 1, max(-1, break_idx - 8), -1)
            for j in candidates:
                if closed.loc[j, "close"] > closed.loc[j, "open"]:
                    obs.append({
                        "type": "BEARISH OB",
                        "direction": "BEARISH",
                        "lower": float(closed.loc[j, "low"]),
                        "upper": float(closed.loc[j, "high"]),
                        "mid": float((closed.loc[j, "low"] + closed.loc[j, "high"]) / 2),
                        "time": closed.loc[j, "time"],
                        "index": j,
                        "source_event": event["event"]
                    })
                    break

    # Deduplicate by type + price range
    unique = []
    seen = set()
    for ob in reversed(obs):
        key = (ob["type"], round(ob["lower"], 8), round(ob["upper"], 8))
        if key not in seen:
            seen.add(key)
            unique.append(ob)

    return list(reversed(unique))


# =========================================================
# STEP 4 — SUPPLY / DEMAND
# =========================================================

def detect_supply_demand(df, events):
    """
    Practical heuristic:
    - Demand: last bearish candle before a bullish structural event.
    - Supply: last bullish candle before a bearish structural event.

    The zones are candle ranges. They are POI candidates, not
    automatic trade signals.
    """
    closed = df.iloc[:-1].copy()
    zones = []

    for event in events[-20:]:
        matches = closed.index[closed["time"] == event["time"]].tolist()
        if not matches:
            continue

        break_idx = matches[0]

        if event["direction"] == "BULLISH":
            for j in range(break_idx - 1, max(-1, break_idx - 8), -1):
                if closed.loc[j, "close"] < closed.loc[j, "open"]:
                    zones.append({
                        "type": "DEMAND",
                        "direction": "BULLISH",
                        "lower": float(closed.loc[j, "low"]),
                        "upper": float(closed.loc[j, "high"]),
                        "mid": float((closed.loc[j, "low"] + closed.loc[j, "high"]) / 2),
                        "time": closed.loc[j, "time"],
                        "index": j
                    })
                    break

        elif event["direction"] == "BEARISH":
            for j in range(break_idx - 1, max(-1, break_idx - 8), -1):
                if closed.loc[j, "close"] > closed.loc[j, "open"]:
                    zones.append({
                        "type": "SUPPLY",
                        "direction": "BEARISH",
                        "lower": float(closed.loc[j, "low"]),
                        "upper": float(closed.loc[j, "high"]),
                        "mid": float((closed.loc[j, "low"] + closed.loc[j, "high"]) / 2),
                        "time": closed.loc[j, "time"],
                        "index": j
                    })
                    break

    unique = []
    seen = set()

    for zone in reversed(zones):
        key = (
            zone["type"],
            round(zone["lower"], 8),
            round(zone["upper"], 8)
        )
        if key not in seen:
            seen.add(key)
            unique.append(zone)

    return list(reversed(unique))


# =========================================================
# POI STATUS
# =========================================================

def price_in_zone(price, zone):
    return zone["lower"] <= price <= zone["upper"]


def distance_to_zone_pct(price, zone):
    if zone["lower"] <= price <= zone["upper"]:
        return 0.0

    if price < zone["lower"]:
        distance = zone["lower"] - price
    else:
        distance = price - zone["upper"]

    return (distance / price) * 100


def add_poi_status(zones, current_price, preferred_direction):
    result = []

    for zone in zones:
        distance = distance_to_zone_pct(current_price, zone)
        inside = price_in_zone(current_price, zone)
        near = distance <= POI_NEAR_PCT

        if inside:
            status = "AT POI"
        elif near:
            status = "NEAR POI"
        else:
            status = "AWAY"

        direction_match = (
            zone["direction"] == preferred_direction
            if preferred_direction in ("BULLISH", "BEARISH")
            else False
        )

        item = dict(zone)
        item["distance_pct"] = distance
        item["inside"] = inside
        item["near"] = near
        item["status"] = status
        item["direction_match"] = direction_match
        result.append(item)

    return result


def select_relevant_pois(
    fvgs,
    order_blocks,
    supply_demand,
    current_price,
    bias
):
    all_pois = []

    all_pois.extend([
        {
            **x,
            "category": "FVG"
        }
        for x in fvgs
    ])

    all_pois.extend([
        {
            **x,
            "category": "ORDER BLOCK"
        }
        for x in order_blocks
    ])

    all_pois.extend([
        {
            **x,
            "category": "SUPPLY/DEMAND"
        }
        for x in supply_demand
    ])

    preferred = [
        x for x in all_pois
        if x["direction"] == bias
    ]

    for x in all_pois:
        x["distance_pct"] = distance_to_zone_pct(current_price, x)
        x["inside"] = price_in_zone(current_price, x)
        x["status"] = (
            "AT POI" if x["inside"]
            else "NEAR POI" if x["distance_pct"] <= POI_NEAR_PCT
            else "AWAY"
        )

    # Prefer bias-aligned POIs, then nearest.
    preferred.sort(key=lambda x: (not x["inside"], x["distance_pct"]))
    all_pois.sort(key=lambda x: (not x["inside"], x["distance_pct"]))

    return preferred[:5], all_pois[:5]


# =========================================================
# DISCORD
# =========================================================

def send_discord_message(message):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret not found!")

    response = requests.post(
        WEBHOOK_URL,
        json={"content": message},
        timeout=10
    )

    if response.status_code not in (200, 204):
        print(f"Discord Error: {response.status_code}")
        print(response.text)
        response.raise_for_status()

    print("✅ Discord message sent successfully!")


# =========================================================
# MAIN
# =========================================================

print()
print("🚀 ICT/SMC ENGINE — STEP 4 POI")
print("============================================")
print(f"📌 Symbol: {DISPLAY_SYMBOL}")
print("⏱️ Timeframe: 1H")
print("📡 Downloading MEXC Futures data...")

df = get_candles(SYMBOL, TIMEFRAME, CANDLE_LIMIT)
print(f"✅ {len(df)} candles loaded")

swing_highs, swing_lows = find_swings(df, SWING_STRENGTH)

events = detect_historical_events(
    df, swing_highs, swing_lows
)

structure = get_current_structure(
    swing_highs, swing_lows, events
)

liquidity = analyze_liquidity(
    df, swing_highs, swing_lows
)

premium_discount = analyze_premium_discount(
    df,
    swing_highs,
    swing_lows,
    structure["bias"]
)

major = premium_discount["major"]
local = premium_discount["local"]

# =========================================================
# POI DETECTION
# =========================================================

fvgs = detect_fvgs(df)

order_blocks = detect_order_blocks(
    df,
    events
)

supply_demand = detect_supply_demand(
    df,
    events
)

current_price = liquidity["current_price"]

preferred_pois, nearest_pois = select_relevant_pois(
    fvgs,
    order_blocks,
    supply_demand,
    current_price,
    structure["bias"]
)

# =========================================================
# TERMINAL
# =========================================================

print()
print("🏗️ CURRENT STRUCTURE")
print(f"📊 Bias: {structure['bias']}")

if structure["last_high"]:
    print(
        f"🔺 High: {structure['last_high']['label']} "
        f"@ ${structure['last_high']['price']:.4f}"
    )

if structure["last_low"]:
    print(
        f"🔻 Low: {structure['last_low']['label']} "
        f"@ ${structure['last_low']['price']:.4f}"
    )

print()
print("🎯 LAST CONFIRMED EVENT")

if events:
    e = events[-1]
    print(f"Event: {e['event']}")
    print(f"Direction: {e['direction']}")
    print(f"Broken Level: ${e['price']:.4f}")
    print(f"Time: {e['time']}")
else:
    print("None")

print()
print("📐 PREMIUM / DISCOUNT")
print(f"Major: {major.get('zone', 'UNKNOWN')}")
print(f"Local: {local.get('zone', 'UNKNOWN')}")
print(f"Alignment: {premium_discount['alignment']}")

print()
print("🎯 STEP 4 — POI")

print(f"FVGs detected: {len(fvgs)}")
print(f"Order Blocks detected: {len(order_blocks)}")
print(f"Supply/Demand zones: {len(supply_demand)}")

print()
print("⭐ RELEVANT POIs")

if preferred_pois:
    for poi in preferred_pois:
        print(
            f"{poi['category']} | "
            f"{poi['type']} | "
            f"${poi['lower']:.4f} - ${poi['upper']:.4f} | "
            f"{poi['status']} | "
            f"{poi['distance_pct']:.3f}% away"
        )
else:
    print("No bias-aligned POI found.")

# =========================================================
# DISCORD MESSAGE
# =========================================================

message = (
    f"🧠 **{DISPLAY_SYMBOL} — ICT/SMC ENGINE**\n\n"
    f"💰 Current Price: `${current_price:.4f}`\n"
    "⏱️ Timeframe: `1H`\n\n"
    "🏗️ **CURRENT STRUCTURE**\n"
    f"📊 Bias: **{structure['bias']}**\n"
)

if structure["last_high"]:
    message += (
        f"🔺 High: **{structure['last_high']['label']}** "
        f"@ `${structure['last_high']['price']:.4f}`\n"
    )

if structure["last_low"]:
    message += (
        f"🔻 Low: **{structure['last_low']['label']}** "
        f"@ `${structure['last_low']['price']:.4f}`\n"
    )

message += "\n🎯 **LAST CONFIRMED EVENT**\n"

if events:
    e = events[-1]
    message += (
        f"Event: **{e['event']}**\n"
        f"Direction: **{e['direction']}**\n"
        f"Broken Level: `${e['price']:.4f}`\n"
        f"Level: `{e['level_type']}`\n"
        f"Time: `{e['time']}`\n"
    )
else:
    message += "Event: `None`\n"

message += (
    "\n💧 **LIQUIDITY**\n"
)

if liquidity["eqh"]:
    message += "🔺 EQH\n"
    for p in liquidity["eqh"][-3:]:
        message += f"• `${p:.4f}`\n"
else:
    message += "🔺 EQH: `None`\n"

if liquidity["eql"]:
    message += "🔻 EQL\n"
    for p in liquidity["eql"][-3:]:
        message += f"• `${p:.4f}`\n"
else:
    message += "🔻 EQL: `None`\n"

if liquidity["bsl"]:
    message += "🟢 BSL\n"
    for p in liquidity["bsl"][-3:]:
        message += f"• `${p:.4f}`\n"
else:
    message += "🟢 BSL: `None`\n"

if liquidity["ssl"]:
    message += "🔴 SSL\n"
    for p in liquidity["ssl"][-3:]:
        message += f"• `${p:.4f}`\n"
else:
    message += "🔴 SSL: `None`\n"

pdh_text = (
    f"${liquidity['pdh']:.4f}"
    if liquidity["pdh"] is not None
    else "None"
)

pdl_text = (
    f"${liquidity['pdl']:.4f}"
    if liquidity["pdl"] is not None
    else "None"
)

message += (
    "\n📅 **PREVIOUS DAY LEVELS**\n"
    f"PDH: `{pdh_text}`\n"
    f"PDL: `{pdl_text}`\n"
)

message += (
    "\n📐 **PREMIUM / DISCOUNT**\n"
    f"Major: **{major.get('zone', 'UNKNOWN')}**\n"
    f"Local: **{local.get('zone', 'UNKNOWN')}**\n"
    f"Alignment: **{premium_discount['alignment']}**\n"
)

message += (
    "\n🎯 **STEP 4 — POI**\n"
    f"FVGs: `{len(fvgs)}`\n"
    f"Order Blocks: `{len(order_blocks)}`\n"
    f"Supply/Demand: `{len(supply_demand)}`\n"
)

if preferred_pois:
    message += "\n⭐ **BIAS-ALIGNED POIs**\n"

    for poi in preferred_pois[:5]:
        message += (
            f"• **{poi['category']}** — "
            f"{poi['type']}\n"
            f"  Zone: `${poi['lower']:.4f}` - `${poi['upper']:.4f}`\n"
            f"  Status: **{poi['status']}**\n"
            f"  Distance: `{poi['distance_pct']:.3f}%`\n"
        )
else:
    message += "\n⭐ Bias-aligned POI: `None detected`\n"

message += (
    "\n⚠️ **POI STATUS IS NOT AN ENTRY SIGNAL**\n"
    "Sweep + Displacement + CHOCH/BOS + Retest "
    "are still required.\n\n"
    "🤖 **ICT/SMC Engine**\n"
    "✅ Structure\n"
    "✅ Historical BOS/CHOCH\n"
    "✅ Liquidity\n"
    "✅ Premium/Discount\n"
    "✅ FVG\n"
    "✅ Order Block\n"
    "✅ Supply/Demand\n"
)

print()
print("📡 Sending Step 4 POI result to Discord...")
send_discord_message(message)

print()
print("✅ STEP 4 POI TEST COMPLETE!")
