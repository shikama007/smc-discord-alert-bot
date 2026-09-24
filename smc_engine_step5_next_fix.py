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
# A POI is called FRESH only while it is unmitigated AND within this
# many fully closed candles from its creation. Older unmitigated POIs
# remain VALID but are labelled AGED_UNMITIGATED instead of FRESH.
POI_FRESH_MAX_AGE = 24

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


def get_current_structure(swing_highs, swing_lows, events, df=None):
    """
    Determine current structure while protecting against a common error:
    using the last mechanically-confirmed pivot as the active structural
    low/high even when a newer protected extreme formed before the latest BOS.

    For the latest bullish BOS, the protected low prefers the most recent
    confirmed swing low formed after the broken swing high; a lowest-candle
    fallback is used only when no confirmed opposite swing exists.

    For the latest bearish BOS/CHOCH, the protected high prefers the most
    recent confirmed swing high formed after the broken swing low; a
    highest-candle fallback is used only when no confirmed opposite swing exists.

    These protected levels are context levels, not automatically HH/HL/LL/LH.
    """

    if events:
        bias = events[-1]["direction"]
    else:
        bias = determine_initial_bias(swing_highs, swing_lows)

    last_high = None
    last_low = None

    # ---------------------------------------------------------
    # CONFIRMED HIGH
    # ---------------------------------------------------------
    if len(swing_highs) >= 2:
        current = swing_highs[-1]["price"]
        previous = swing_highs[-2]["price"]
        label = (
            "HH" if current > previous
            else "LH" if current < previous
            else "EQH"
        )
        last_high = {
            "label": label,
            "price": current,
            "time": swing_highs[-1]["time"]
        }

    # ---------------------------------------------------------
    # CONFIRMED LOW
    # ---------------------------------------------------------
    if len(swing_lows) >= 2:
        current = swing_lows[-1]["price"]
        previous = swing_lows[-2]["price"]
        label = (
            "HL" if current > previous
            else "LL" if current < previous
            else "EQL"
        )
        last_low = {
            "label": label,
            "price": current,
            "time": swing_lows[-1]["time"]
        }

    # ---------------------------------------------------------
    # PROTECTED STRUCTURE FIX
    # ---------------------------------------------------------
    # The latest BOS/CHOCH is the important anchor. The protected
    # opposite extreme that led into that break should not be replaced
    # by a later/less-relevant mechanical pivot.
    if df is not None and events:
        closed = df.iloc[:-1].copy().reset_index(drop=True)
        event = events[-1]

        if not closed.empty:
            event_rows = closed[closed["time"] == event["time"]]

            if not event_rows.empty:
                event_idx = int(event_rows.index[-1])

                if event["direction"] == "BULLISH":
                    # Find the swing high that was broken.
                    broken_highs = [
                        s for s in swing_highs
                        if abs(float(s["price"]) - float(event["price"])) < 1e-9
                        and s["index"] < event_idx
                    ]

                    if broken_highs:
                        anchor_idx = broken_highs[-1]["index"]

                        # Prefer the MOST RECENT CONFIRMED swing low
                        # between the broken high and the BOS candle.
                        # This is closer to structural ICT logic than
                        # simply taking the lowest wick in the whole leg.
                        opposite_swings = [
                            s for s in swing_lows
                            if anchor_idx < s["index"] <= event_idx
                        ]

                        if opposite_swings:
                            protected = opposite_swings[-1]
                            protected_price = float(protected["price"])
                            protected_idx = int(protected["index"])
                        else:
                            # Fallback only when no confirmed opposite
                            # swing exists in the BOS leg.
                            segment = closed.iloc[anchor_idx:event_idx + 1]
                            protected_idx = int(segment["low"].idxmin())
                            protected_price = float(
                                closed.loc[protected_idx, "low"]
                            )

                        last_low = {
                            "label": "PROTECTED LOW",
                            "price": protected_price,
                            "time": closed.loc[protected_idx, "time"]
                        }

                elif event["direction"] == "BEARISH":
                    # Find the swing low that was broken.
                    broken_lows = [
                        s for s in swing_lows
                        if abs(float(s["price"]) - float(event["price"])) < 1e-9
                        and s["index"] < event_idx
                    ]

                    if broken_lows:
                        anchor_idx = broken_lows[-1]["index"]

                        # Prefer the MOST RECENT CONFIRMED swing high
                        # between the broken low and the BOS/CHOCH candle.
                        opposite_swings = [
                            s for s in swing_highs
                            if anchor_idx < s["index"] <= event_idx
                        ]

                        if opposite_swings:
                            protected = opposite_swings[-1]
                            protected_price = float(protected["price"])
                            protected_idx = int(protected["index"])
                        else:
                            segment = closed.iloc[anchor_idx:event_idx + 1]
                            protected_idx = int(segment["high"].idxmax())
                            protected_price = float(
                                closed.loc[protected_idx, "high"]
                            )

                        last_high = {
                            "label": "PROTECTED HIGH",
                            "price": protected_price,
                            "time": closed.loc[protected_idx, "time"]
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

    # Discord message limit = 2000 characters.
    max_length = 1900
    chunks = []

    while len(message) > max_length:
        split_at = message.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(message[:split_at])
        message = message[split_at:].lstrip()

    if message:
        chunks.append(message)

    for number, chunk in enumerate(chunks, start=1):
        response = requests.post(
            WEBHOOK_URL,
            json={"content": chunk},
            timeout=10
        )

        print(
            f"📡 Discord message {number}/{len(chunks)} "
            f"→ {response.status_code}"
        )

        if response.status_code not in (200, 204):
            print(f"Discord Error: {response.status_code}")
            print(response.text)
            response.raise_for_status()

    print(f"✅ Discord alert sent ({len(chunks)} message(s))!")



# =========================================================
# STEP 4 QUALITY UPGRADE
# =========================================================
# The functions below replace the raw POI selection logic.
# They keep fresh, bias-aligned and context-relevant POIs,
# then rank them by a simple quality score. Older unmitigated POIs
# remain valid for historical context but are not labelled FRESH.
#
# IMPORTANT:
# - This is a mechanical approximation of discretionary ICT/SMC POI work.
# - It is NOT an entry signal.
# - A POI must still be validated by Sweep -> Displacement -> CHOCH/BOS -> Retest.


def zone_overlap(a, b):
    lower = max(a["lower"], b["lower"])
    upper = min(a["upper"], b["upper"])
    if lower > upper:
        return None

    return {
        "lower": lower,
        "upper": upper,
        "size": upper - lower
    }


def zone_mid_distance_pct(price, zone):
    mid = (zone["lower"] + zone["upper"]) / 2
    return abs(price - mid) / price * 100


def _poi_age_candles(poi, closed):
    """Number of fully closed candles since a POI was created."""
    if closed.empty or poi.get("time") is None:
        return 0

    matches = closed.index[closed["time"] == poi["time"]].tolist()
    if not matches:
        # If the exact timestamp is not present, use a conservative
        # fallback based on time ordering.
        later = closed[closed["time"] >= poi["time"]]
        if later.empty:
            return 0
        created_idx = int(later.index[0])
    else:
        created_idx = int(matches[0])

    latest_idx = int(closed.index[-1])
    return max(0, latest_idx - created_idx)


def _set_poi_freshness(poi, age_candles, mitigated):
    """Assign explicit freshness state without deleting valid old POIs."""
    poi["age_candles"] = int(age_candles)
    poi["mitigated"] = bool(mitigated)

    if mitigated:
        poi["fresh"] = False
        poi["freshness_status"] = "MITIGATED"
    elif age_candles <= POI_FRESH_MAX_AGE:
        poi["fresh"] = True
        poi["freshness_status"] = "FRESH"
    else:
        # Still potentially valid because price has not invalidated it,
        # but it is no longer labelled fresh.
        poi["fresh"] = False
        poi["freshness_status"] = "AGED_UNMITIGATED"


def mark_fvg_quality(fvgs, df):
    closed = df.iloc[:-1].copy()

    for fvg in fvgs:
        later = closed[closed["time"] > fvg["time"]]

        if fvg["direction"] == "BULLISH":
            mitigated = (
                not later.empty
                and float(later["low"].min()) <= fvg["lower"]
            )
        else:
            mitigated = (
                not later.empty
                and float(later["high"].max()) >= fvg["upper"]
            )

        age = _poi_age_candles(fvg, closed)
        _set_poi_freshness(fvg, age, mitigated)

    return fvgs


def mark_ob_quality(order_blocks, df):
    closed = df.iloc[:-1].copy()

    for ob in order_blocks:
        later = closed[closed["time"] > ob["time"]]

        if ob["direction"] == "BULLISH":
            mitigated = (
                not later.empty
                and float(later["close"].min()) < ob["lower"]
            )
        else:
            mitigated = (
                not later.empty
                and float(later["close"].max()) > ob["upper"]
            )

        age = _poi_age_candles(ob, closed)
        _set_poi_freshness(ob, age, mitigated)

    return order_blocks


def mark_zone_quality(zones, df):
    closed = df.iloc[:-1].copy()

    for zone in zones:
        later = closed[closed["time"] > zone["time"]]

        if zone["direction"] == "BULLISH":
            mitigated = (
                not later.empty
                and float(later["close"].min()) < zone["lower"]
            )
        else:
            mitigated = (
                not later.empty
                and float(later["close"].max()) > zone["upper"]
            )

        age = _poi_age_candles(zone, closed)
        _set_poi_freshness(zone, age, mitigated)

    return zones


def calculate_poi_score(
    poi,
    current_price,
    bias,
    pd_alignment,
    latest_event,
    premium_discount
):
    score = 0
    reasons = []

    # 1) Direction aligned with current structural bias.
    if poi["direction"] == bias:
        score += 25
        reasons.append("BIAS ALIGNED")

    # 2) Freshness is time-aware. An older but still unmitigated POI
    # remains valid, but does not receive the same bonus as a fresh POI.
    freshness = poi.get("freshness_status", "FRESH" if poi.get("fresh", False) else "MITIGATED")
    if freshness == "FRESH":
        score += 20
        reasons.append("FRESH")
    elif freshness == "AGED_UNMITIGATED":
        score += 5
        reasons.append("AGED UNMITIGATED")
    else:
        score -= 30
        reasons.append("MITIGATED")

    # 3) Current price interaction.
    if poi["inside"]:
        score += 25
        reasons.append("AT POI")
    elif poi["distance_pct"] <= POI_NEAR_PCT:
        score += 18
        reasons.append("NEAR POI")
    elif poi["distance_pct"] <= 1.0:
        score += 8
        reasons.append("WITHIN 1%")

    # 4) Premium/discount alignment.
    # Use the actual major/local equilibrium values instead of
    # a generic numeric test.
    major = premium_discount.get("major", {})
    local = premium_discount.get("local", {})

    pd_aligned = False

    if bias == "BEARISH":
        if major.get("status") == "OK" and poi["mid"] >= major["equilibrium"]:
            pd_aligned = True
        if local.get("status") == "OK" and poi["mid"] >= local["equilibrium"]:
            pd_aligned = True

    elif bias == "BULLISH":
        if major.get("status") == "OK" and poi["mid"] <= major["equilibrium"]:
            pd_aligned = True
        if local.get("status") == "OK" and poi["mid"] <= local["equilibrium"]:
            pd_aligned = True

    if pd_aligned:
        score += 10
        reasons.append("PD ALIGNED")

    # 5) Recent structural event gets a small boost when the POI
    # belongs to the same direction.
    if latest_event and latest_event["direction"] == poi["direction"]:
        score += 10
        reasons.append("EVENT ALIGNED")

    return score, reasons


def build_quality_pois(
    fvgs,
    order_blocks,
    supply_demand,
    current_price,
    bias,
    premium_discount,
    events
):
    candidates = []

    for x in fvgs:
        candidates.append({**x, "category": "FVG"})

    for x in order_blocks:
        candidates.append({**x, "category": "ORDER BLOCK"})

    for x in supply_demand:
        candidates.append({**x, "category": "SUPPLY/DEMAND"})

    latest_event = events[-1] if events else None
    pd_alignment = premium_discount.get("alignment", "UNKNOWN")

    # Calculate basic proximity and status.
    for poi in candidates:
        poi["distance_pct"] = distance_to_zone_pct(current_price, poi)
        poi["inside"] = price_in_zone(current_price, poi)

        if poi["inside"]:
            poi["status"] = "AT POI"
        elif poi["distance_pct"] <= POI_NEAR_PCT:
            poi["status"] = "NEAR POI"
        else:
            poi["status"] = "AWAY"

        # This is used only as a compact context flag.
        # Actual PD zone is already calculated by the main engine.
        poi["mid"] = (poi["lower"] + poi["upper"]) / 2

        score, reasons = calculate_poi_score(
            poi,
            current_price,
            bias,
            pd_alignment,
            latest_event,
            premium_discount
        )

        poi["score"] = score
        poi["reasons"] = reasons

    # Confluence: overlapping FVG + OB is more useful than isolated
    # raw candidates, so give both zones a bonus.
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a = candidates[i]
            b = candidates[j]

            if {a["category"], b["category"]} == {"FVG", "ORDER BLOCK"}:
                overlap = zone_overlap(a, b)

                if overlap and overlap["size"] > 0:
                    a["confluence"] = True
                    b["confluence"] = True
                    a["score"] += 15
                    b["score"] += 15

                    if "FVG + OB CONFLUENCE" not in a["reasons"]:
                        a["reasons"].append("FVG + OB CONFLUENCE")
                    if "FVG + OB CONFLUENCE" not in b["reasons"]:
                        b["reasons"].append("FVG + OB CONFLUENCE")

                    a["overlap_zone"] = overlap
                    b["overlap_zone"] = overlap

    for poi in candidates:
        poi.setdefault("confluence", False)

    # Only bias-aligned, fresh POIs are eligible for the main shortlist.
    eligible = [
        p for p in candidates
        if p["direction"] == bias and p.get("fresh", False)
    ]

    eligible.sort(
        key=lambda p: (
            -p["score"],
            p["distance_pct"],
            p["time"]
        )
    )

    # Remove duplicate zones with same category/type and nearly identical range.
    selected = []
    seen = set()

    for poi in eligible:
        key = (
            poi["category"],
            poi["type"],
            round(poi["lower"], 3),
            round(poi["upper"], 3)
        )

        if key in seen:
            continue

        seen.add(key)
        selected.append(poi)

        if len(selected) >= 3:
            break

    # A compact list for diagnostics.
    all_ranked = sorted(
        candidates,
        key=lambda p: (-p["score"], p["distance_pct"])
    )

    return selected, all_ranked


def select_relevant_pois(
    fvgs,
    order_blocks,
    supply_demand,
    current_price,
    bias,
    premium_discount=None,
    events=None,
    df=None
):
    premium_discount = premium_discount or {"alignment": "UNKNOWN"}
    events = events or []

    # Apply freshness / mitigation filters.
    if df is not None:
        fvgs = mark_fvg_quality(fvgs, df)
        order_blocks = mark_ob_quality(order_blocks, df)
        supply_demand = mark_zone_quality(supply_demand, df)

    return build_quality_pois(
        fvgs,
        order_blocks,
        supply_demand,
        current_price,
        bias,
        premium_discount,
        events
    )



# =========================================================
# STEP 5 — LIQUIDITY SWEEP DETECTION
# =========================================================
# A sweep is treated as a wick through a pre-existing liquidity level
# followed by a close back through that level.
#
# Bearish sweep:
#   high > BSL/EQH/PDH and close < the level
#
# Bullish sweep:
#   low < SSL/EQL/PDL and close > the level
#
# IMPORTANT:
# - Only fully closed candles are analysed.
# - The liquidity level must already exist before the sweep candle.
# - A sweep is NOT an entry signal.
# - POI interaction requires the sweep candle to actually enter the POI.
# - Step 6 will validate
#   displacement after a confirmed sweep.

SWEEP_LOOKBACK = 30
# A sweep remains part of the active ICT sequence only for a limited
# number of closed candles. Older sweeps are kept for diagnostics but
# are not treated as the active Step 5 setup.
SWEEP_ACTIVE_MAX_AGE = 6
SWEEP_MIN_PENETRATION_PCT = 0.02
SWEEP_POI_NEAR_PCT = 0.50


def _previous_day_levels_for_candle(closed_df, candle_index):
    """Return the previous calendar day's high/low for this candle."""
    if candle_index <= 0:
        return None, None

    candle_date = closed_df.iloc[candle_index]["time"].date()
    previous_rows = closed_df[closed_df["time"].dt.date < candle_date]

    if previous_rows.empty:
        return None, None

    previous_date = previous_rows["time"].dt.date.max()
    previous_day = previous_rows[previous_rows["time"].dt.date == previous_date]

    if previous_day.empty:
        return None, None

    return (
        float(previous_day["high"].max()),
        float(previous_day["low"].min())
    )


def _level_touched(price, level, direction):
    if level is None or level <= 0:
        return False

    penetration = abs(price - level) / level * 100

    if penetration < SWEEP_MIN_PENETRATION_PCT:
        return False

    if direction == "BEARISH":
        return price > level
    return price < level


def detect_liquidity_sweeps(
    df,
    swing_highs,
    swing_lows,
    preferred_pois=None,
    all_pois=None,
    lookback=SWEEP_LOOKBACK
):
    """
    Detect confirmed liquidity sweeps from closed candles.

    Improvements:
    - Liquidity levels must exist before the sweep candle.
    - EQH/EQL are valid sweep targets, not just BSL/SSL/PDH/PDL.
    - POI interaction is time-consistent: the POI must already exist
      at or before the sweep candle.
    - Only the sweep candle's own range can confirm POI interaction.
    - Sweep age is stored so Step 5 can distinguish active vs stale setups.
    """
    closed = df.iloc[:-1].copy().reset_index(drop=True)

    if closed.empty:
        return []

    start = max(0, len(closed) - lookback)
    sweeps = []

    # Keep both lists:
    # - preferred_pois = current high-quality POIs
    # - all_pois = every detected POI, useful for historical time-correctness
    preferred_pois = preferred_pois or []
    all_pois = all_pois if all_pois is not None else preferred_pois

    # ---------------------------------------------------------
    # Historical EQH/EQL helpers
    # ---------------------------------------------------------
    def historical_equal_high_levels(prior_highs):
        levels = []
        for j in range(len(prior_highs) - 1):
            a = prior_highs[j]
            b = prior_highs[j + 1]

            if prices_are_equal(a["price"], b["price"]):
                levels.append({
                    "price": (float(a["price"]) + float(b["price"])) / 2,
                    "type": "EQH",
                    "source_time": b["time"]
                })
        return levels

    def historical_equal_low_levels(prior_lows):
        levels = []
        for j in range(len(prior_lows) - 1):
            a = prior_lows[j]
            b = prior_lows[j + 1]

            if prices_are_equal(a["price"], b["price"]):
                levels.append({
                    "price": (float(a["price"]) + float(b["price"])) / 2,
                    "type": "EQL",
                    "source_time": b["time"]
                })
        return levels

    for i in range(start, len(closed)):
        candle = closed.iloc[i]
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        candle_time = candle["time"]

        # Only levels confirmed/known before this candle are eligible.
        prior_highs = [
            s for s in swing_highs
            if s["index"] < i
        ]
        prior_lows = [
            s for s in swing_lows
            if s["index"] < i
        ]

        targets_high = []
        targets_low = []

        for s in prior_highs:
            targets_high.append({
                "price": float(s["price"]),
                "type": "BSL",
                "source_time": s["time"]
            })

        for s in prior_lows:
            targets_low.append({
                "price": float(s["price"]),
                "type": "SSL",
                "source_time": s["time"]
            })

        # EQH/EQL must be formed from swings that already existed.
        targets_high.extend(historical_equal_high_levels(prior_highs))
        targets_low.extend(historical_equal_low_levels(prior_lows))

        # Previous-day high/low are valid only after that day is complete.
        pdh, pdl = _previous_day_levels_for_candle(closed, i)

        if pdh is not None:
            targets_high.append({
                "price": pdh,
                "type": "PDH",
                "source_time": None
            })

        if pdl is not None:
            targets_low.append({
                "price": pdl,
                "type": "PDL",
                "source_time": None
            })

        # De-duplicate same-type/near-identical levels while preserving
        # the liquidity type. EQH/EQL remain separate from ordinary BSL/SSL.
        def dedupe_targets(targets):
            unique = []

            for target in sorted(
                targets,
                key=lambda x: (x["price"], x["type"])
            ):
                duplicate = False

                for existing in unique:
                    same_type = existing["type"] == target["type"]
                    close_level = prices_are_equal(
                        existing["price"],
                        target["price"]
                    )

                    if same_type and close_level:
                        duplicate = True
                        break

                if not duplicate:
                    unique.append(target)

            return unique

        unique_highs = dedupe_targets(targets_high)
        unique_lows = dedupe_targets(targets_low)

        # -----------------------------------------------------
        # POI helper
        # -----------------------------------------------------
        def find_historical_poi(direction):
            candidates = []

            for poi in all_pois:
                if poi.get("direction") != direction:
                    continue

                poi_time = poi.get("time")

                # Critical anti-lookahead rule:
                # the POI must already exist by the sweep candle.
                if poi_time is not None and poi_time > candle_time:
                    continue

                candle_touches_poi = (
                    high >= float(poi["lower"])
                    and low <= float(poi["upper"])
                )

                if not candle_touches_poi:
                    continue

                candidates.append(poi)

            if not candidates:
                return None

            # Prefer the most relevant historical POI:
            # 1) fresh-at-current-time if available
            # 2) higher score if available
            # 3) newest POI that already existed at sweep time
            candidates.sort(
                key=lambda p: (
                    bool(p.get("fresh", False)),
                    float(p.get("score", 0)),
                    p.get("time")
                ),
                reverse=True
            )

            return candidates[0]

        # -----------------------------------------------------
        # BEARISH SWEEP — buy-side liquidity taken, close back below
        # -----------------------------------------------------
        for target in unique_highs:
            level = float(target["price"])

            if high > level and close < level:
                penetration_pct = (high - level) / level * 100

                if penetration_pct < SWEEP_MIN_PENETRATION_PCT:
                    continue

                historical_poi = find_historical_poi("BEARISH")

                sweeps.append({
                    "direction": "BEARISH",
                    "liquidity": target["type"],
                    "level": level,
                    "high": high,
                    "low": low,
                    "close": close,
                    "penetration_pct": penetration_pct,
                    "time": candle_time,
                    "candle_index": i,
                    "age_candles": 0,
                    "poi_interaction": historical_poi is not None,
                    "poi": historical_poi
                })

        # -----------------------------------------------------
        # BULLISH SWEEP — sell-side liquidity taken, close back above
        # -----------------------------------------------------
        for target in unique_lows:
            level = float(target["price"])

            if low < level and close > level:
                penetration_pct = (level - low) / level * 100

                if penetration_pct < SWEEP_MIN_PENETRATION_PCT:
                    continue

                historical_poi = find_historical_poi("BULLISH")

                sweeps.append({
                    "direction": "BULLISH",
                    "liquidity": target["type"],
                    "level": level,
                    "high": high,
                    "low": low,
                    "close": close,
                    "penetration_pct": penetration_pct,
                    "time": candle_time,
                    "candle_index": i,
                    "age_candles": 0,
                    "poi_interaction": historical_poi is not None,
                    "poi": historical_poi
                })

    # Age is measured from the latest fully closed candle.
    latest_closed_index = len(closed) - 1

    for sweep in sweeps:
        sweep["age_candles"] = max(
            0,
            latest_closed_index - int(sweep["candle_index"])
        )
        sweep["active"] = (
            sweep["age_candles"] <= SWEEP_ACTIVE_MAX_AGE
        )

    # Newest first.
    sweeps.sort(
        key=lambda x: (
            x["time"],
            -x["penetration_pct"]
        ),
        reverse=True
    )

    return sweeps[:10]



def classify_sweep_state(sweep, events):
    """
    Classify what happened after a sweep. This prevents an already-followed
    sweep from being reported simply as an "ACTIVE SWEEP" forever.

    States are descriptive only:
      SWEEP_ONLY
      SWEEP_PLUS_FOLLOW_THROUGH
      SWEEP_PLUS_OPPOSITE_EVENT
    """
    if sweep is None:
        return {"state": "NO_SWEEP", "follow_event": None}

    later = [
        e for e in events
        if e.get("time") is not None
        and e["time"] > sweep["time"]
    ]

    same_direction = [
        e for e in later
        if e.get("direction") == sweep.get("direction")
    ]

    opposite_direction = [
        e for e in later
        if e.get("direction") != sweep.get("direction")
    ]

    if same_direction:
        event = same_direction[0]
        return {
            "state": "SWEEP_PLUS_FOLLOW_THROUGH",
            "follow_event": event
        }

    if opposite_direction:
        event = opposite_direction[0]
        return {
            "state": "SWEEP_PLUS_OPPOSITE_EVENT",
            "follow_event": event
        }

    return {"state": "SWEEP_ONLY", "follow_event": None}


def get_step5_status(sweeps, bias, preferred_pois=None, events=None):
    """
    Summarise Step 5 while separating an ACTIVE sweep from an old
    historical sweep. This prevents a 20-30 candle-old sweep from
    being carried forward into a new displacement/retest sequence.
    """
    preferred_pois = preferred_pois or []

    if not sweeps:
        return {
            "status": "NO CONFIRMED SWEEP",
            "latest": None,
            "latest_any": None,
            "bias_aligned": False,
            "poi_aligned": False
        }

    latest_any = sweeps[0]
    active_sweeps = [
        s for s in sweeps
        if s.get("active", False)
    ]

    if not active_sweeps:
        return {
            "status": "STALE SWEEP — NO ACTIVE SWEEP",
            "latest": None,
            "latest_any": latest_any,
            "bias_aligned": False,
            "poi_aligned": False
        }

    latest = active_sweeps[0]
    bias_aligned = latest["direction"] == bias
    poi_aligned = bool(latest.get("poi_interaction"))
    state = classify_sweep_state(latest, events=events)
    latest["sequence_state"] = state["state"]
    latest["follow_event"] = state["follow_event"]

    if state["state"] == "SWEEP_PLUS_FOLLOW_THROUGH":
        status = "SWEEP + FOLLOW-THROUGH EVENT"
    elif state["state"] == "SWEEP_PLUS_OPPOSITE_EVENT":
        status = "SWEEP + OPPOSITE EVENT"
    elif bias_aligned and poi_aligned:
        status = "ACTIVE SWEEP + ACTUAL POI INTERACTION"
    elif bias_aligned:
        status = "ACTIVE BIAS-ALIGNED SWEEP"
    else:
        status = "ACTIVE COUNTER-BIAS SWEEP"

    return {
        "status": status,
        "latest": latest,
        "latest_any": latest_any,
        "bias_aligned": bias_aligned,
        "poi_aligned": poi_aligned,
        "sequence_state": state["state"],
        "follow_event": state["follow_event"]
    }


# =========================================================
# DEVELOPING / UNCONFIRMED SWING CONTEXT
# =========================================================
# Confirmed swings require right-side candle confirmation.
# We keep confirmed structure untouched and show newer extremes
# separately as UNCONFIRMED so the chart context is clearer.

def get_developing_extremes(
    df,
    confirmed_swing_highs,
    confirmed_swing_lows,
    lookback=24
):
    """
    Detect recent unconfirmed extremes after the latest confirmed swing.

    Improvements:
    - Uses a wider recent context (24 closed candles).
    - Searches from the latest confirmed swing forward.
    - Does not use the current/forming candle.
    - Does not turn an unconfirmed extreme into HH/LH/LL/HL.
    - Keeps historical extremes outside the current context out.
    """

    closed = df.iloc[:-1].copy()

    if closed.empty:
        return {"high": None, "low": None}

    latest_high = confirmed_swing_highs[-1] if confirmed_swing_highs else None
    latest_low = confirmed_swing_lows[-1] if confirmed_swing_lows else None

    recent_start = max(0, len(closed) - lookback)

    developing_high = None
    developing_low = None

    # ---------------------------------------------------------
    # DEVELOPING HIGH
    # ---------------------------------------------------------
    # Start after the latest confirmed high, but never outside
    # the recent context window.
    high_start = recent_start

    if latest_high is not None:
        high_start = max(recent_start, latest_high["index"] + 1)

    if high_start < len(closed):
        high_window = closed.iloc[high_start:]
        high_idx = int(high_window["high"].idxmax())
        high_price = float(closed.loc[high_idx, "high"])

        confirmed_high_price = (
            float(latest_high["price"])
            if latest_high is not None
            else None
        )

        if (
            confirmed_high_price is None
            or high_price > confirmed_high_price
        ):
            developing_high = {
                "price": high_price,
                "time": closed.loc[high_idx, "time"],
                "index": high_idx,
                "status": "UNCONFIRMED"
            }

    # ---------------------------------------------------------
    # DEVELOPING LOW
    # ---------------------------------------------------------
    low_start = recent_start

    if latest_low is not None:
        low_start = max(recent_start, latest_low["index"] + 1)

    if low_start < len(closed):
        low_window = closed.iloc[low_start:]
        low_idx = int(low_window["low"].idxmin())
        low_price = float(closed.loc[low_idx, "low"])

        confirmed_low_price = (
            float(latest_low["price"])
            if latest_low is not None
            else None
        )

        if (
            confirmed_low_price is None
            or low_price < confirmed_low_price
        ):
            developing_low = {
                "price": low_price,
                "time": closed.loc[low_idx, "time"],
                "index": low_idx,
                "status": "UNCONFIRMED"
            }

    return {
        "high": developing_high,
        "low": developing_low
    }


# =========================================================
# MAIN
# =========================================================

print()
print("🚀 ICT/SMC ENGINE — STEP 5 FINAL FIXED")
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
    swing_highs,
    swing_lows,
    events,
    df=df
)

developing = get_developing_extremes(
    df,
    swing_highs,
    swing_lows
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
    structure["bias"],
    premium_discount=premium_discount,
    events=events,
    df=df
)

# All detected POIs are kept separately for historical Step 5 analysis.
# This prevents current-time freshness filtering from creating a
# look-ahead error when we evaluate an older sweep.
all_pois = (
    [{**x, "category": "FVG"} for x in fvgs]
    + [{**x, "category": "ORDER BLOCK"} for x in order_blocks]
    + [{**x, "category": "SUPPLY/DEMAND"} for x in supply_demand]
)

# =========================================================
# STEP 5 — LIQUIDITY SWEEP
# =========================================================

sweeps = detect_liquidity_sweeps(
    df,
    swing_highs,
    swing_lows,
    preferred_pois=preferred_pois,
    all_pois=all_pois
)

step5 = get_step5_status(
    sweeps,
    structure["bias"],
    preferred_pois=preferred_pois,
    events=events
)

# =========================================================
# TERMINAL
# =========================================================

print()
print("🏗️ CURRENT STRUCTURE")
print(f"📊 Bias: {structure['bias']}")

if structure["last_high"]:
    print(
        f"🔺 Confirmed High: {structure['last_high']['label']} "
        f"@ ${structure['last_high']['price']:.4f}"
    )

if structure["last_low"]:
    print(
        f"🔻 Confirmed Low: {structure['last_low']['label']} "
        f"@ ${structure['last_low']['price']:.4f}"
    )

if developing["high"]:
    print(
        f"🟡 Developing High: ~${developing['high']['price']:.4f} "
        f"({developing['high']['status']})"
    )

if developing["low"]:
    print(
        f"🟡 Developing Low: ~${developing['low']['price']:.4f} "
        f"({developing['low']['status']})"
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
print("⭐ CURRENT-TIME TOP QUALITY POIs")

if preferred_pois:
    for poi in preferred_pois:
        print(
            f"{poi['category']} | "
            f"{poi['type']} | "
            f"${poi['lower']:.4f} - ${poi['upper']:.4f} | "
            f"{poi['status']} | "
            f"Score: {poi['score']} | "
            f"Freshness: {poi.get('freshness_status', 'UNKNOWN')} | "
            f"Age: {poi.get('age_candles', 0)} | "
            f"Confluence: {poi['confluence']}"
        )
else:
    print("No fresh bias-aligned POI found.")

print()
print("📊 RAW → QUALITY FILTER")
print(f"Raw FVGs: {len(fvgs)}")
print(f"Raw OBs: {len(order_blocks)}")
print(f"Raw Supply/Demand: {len(supply_demand)}")
print(f"Final quality POIs: {len(preferred_pois)}")

print()
print("🧹 STEP 5 — LIQUIDITY SWEEP")
print(f"Status: {step5['status']}")

if step5["latest"]:
    sweep = step5["latest"]
    print(f"Direction: {sweep['direction']}")
    print(f"Liquidity: {sweep['liquidity']}")
    print(f"Level: ${sweep['level']:.4f}")
    print(f"Sweep High: ${sweep['high']:.4f}")
    print(f"Sweep Low: ${sweep['low']:.4f}")
    print(f"Close Back: ${sweep['close']:.4f}")
    print(f"Penetration: {sweep['penetration_pct']:.3f}%")
    print(f"POI Interaction: {'YES' if sweep['poi_interaction'] else 'NO'}")
    print(f"Age: {sweep['age_candles']} closed candle(s)")
    print(f"Active Window: {'YES' if sweep['active'] else 'NO'}")
    print(f"Sequence State: {sweep.get('sequence_state', 'SWEEP_ONLY')}")
    if sweep.get("follow_event"):
        fe = sweep["follow_event"]
        print(f"Follow-through Event: {fe['event']} {fe['direction']} @ ${fe['price']:.4f}")
        print(f"Follow-through Time: {fe['time']}")
    print(f"Time: {sweep['time']}")
else:
    if step5.get("latest_any"):
        stale = step5["latest_any"]
        print(
            f"Latest historical sweep: {stale['direction']} "
            f"{stale['liquidity']} @ ${stale['level']:.4f} | "
            f"Age: {stale['age_candles']} candles"
        )
    else:
        print("No confirmed liquidity sweep in recent closed candles.")

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
        f"🔻 Confirmed Low: **{structure['last_low']['label']}** "
        f"@ `${structure['last_low']['price']:.4f}`\n"
    )

if developing["high"]:
    message += (
        f"🟡 Developing High: `~${developing['high']['price']:.4f}` "
        f"— **UNCONFIRMED**\n"
    )

if developing["low"]:
    message += (
        f"🟡 Developing Low: `~${developing['low']['price']:.4f}` "
        f"— **UNCONFIRMED**\n"
    )

message += (
    "\n🧠 **STRUCTURE NOTE**\n"
    "Confirmed swings require right-side candle confirmation. "
    "Developing highs/lows are searched only after the latest "
    "confirmed swing and within the recent 24-closed-candle context window. "
    "They are **NOT** used as confirmed structure.\n"
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
    message += "\n⭐ **CURRENT-TIME TOP QUALITY POIs**\n"

    for poi in preferred_pois[:3]:
        freshness_text = poi.get("freshness_status", "FRESH" if poi.get("fresh", False) else "MITIGATED")
        confluence_text = "YES" if poi.get("confluence", False) else "NO"

        message += (
            f"• **{poi['category']}** — {poi['type']}\n"
            f"  Zone: `${poi['lower']:.4f}` - `${poi['upper']:.4f}`\n"
            f"  Status: **{poi['status']}**\n"
            f"  Distance: `{poi['distance_pct']:.3f}%`\n"
            f"  Score: **{poi['score']}**\n"
            f"  Freshness: `{freshness_text}` | Age: `{poi.get('age_candles', 0)} candles` | Confluence: `{confluence_text}`\n"
            f"  Why: `{', '.join(poi['reasons'])}`\n"
        )
else:
    message += "\n⭐ Fresh bias-aligned POI: `None detected`\n"

message += (
    f"\n📊 Raw POIs → `{len(fvgs)} FVG / {len(order_blocks)} OB / {len(supply_demand)} S-D`\n"
    f"🎯 Final Quality POIs → `{len(preferred_pois)}`\n"
)

message += (
    "\n🧹 **STEP 5 — LIQUIDITY SWEEP**\n"
    f"Status: **{step5['status']}**\n"
)

if step5["latest"]:
    sweep = step5["latest"]
    poi_text = "YES" if sweep["poi_interaction"] else "NO"

    message += (
        f"Direction: **{sweep['direction']}**\n"
        f"Liquidity: `{sweep['liquidity']}`\n"
        f"Level: `${sweep['level']:.4f}`\n"
        f"Sweep High: `${sweep['high']:.4f}`\n"
        f"Sweep Low: `${sweep['low']:.4f}`\n"
        f"Close Back: `${sweep['close']:.4f}`\n"
        f"Penetration: `{sweep['penetration_pct']:.3f}%`\n"
        f"POI Interaction: **{poi_text}**\n"
        f"Age: `{sweep['age_candles']} closed candle(s)`\n"
        f"Active Window: **{'YES' if sweep['active'] else 'NO'}**\n"
        f"Sequence State: **{sweep.get('sequence_state', 'SWEEP_ONLY')}**\n"
    )

    if sweep.get("follow_event"):
        fe = sweep["follow_event"]
        message += (
            f"Follow-through Event: **{fe['event']} {fe['direction']}**\n"
            f"Follow-through Level: `${fe['price']:.4f}`\n"
            f"Follow-through Time: `{fe['time']}`\n"
        )

    message += f"Time: `{sweep['time']}`\n"

    if sweep.get("poi") is not None:
        poi = sweep["poi"]
        poi_time = poi.get("time")
        message += (
            f"Sweep POI Zone: `${poi['lower']:.4f}` - `${poi['upper']:.4f}`\n"
            f"Sweep POI Type: `{poi['category']} / {poi['type']}`\n"
            f"Sweep POI Created: `{poi_time}`\n"
        )
else:
    if step5.get("latest_any"):
        stale = step5["latest_any"]
        message += (
            f"Latest historical sweep: `{stale['direction']} "
            f"{stale['liquidity']} @ ${stale['level']:.4f}`\n"
            f"Age: `{stale['age_candles']} closed candle(s)`\n"
            "⚠️ No active Step 5 sweep inside the current sweep window.\n"
        )
    else:
        message += "No confirmed liquidity sweep in recent closed candles.\n"

message += (
    "\n⚠️ **POI STATUS IS NOT AN ENTRY SIGNAL**\n"
    "Fresh = unmitigated and <= 24 closed candles old. Older unmitigated POIs are labelled AGED_UNMITIGATED.\n"
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
    "✅ Liquidity Sweep (Step 5)\n"
)

print()
print("📡 Sending Step 5 result to Discord...")
send_discord_message(message)

print()
print("✅ STEP 5 LIQUIDITY SWEEP TEST COMPLETE!")
