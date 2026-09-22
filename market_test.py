import requests
import pandas as pd
import os

# ==============================
# SETTINGS
# ==============================

SYMBOL = "LTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


# ==============================
# GET FUTURES KLINES
# ==============================

def get_candles(symbol, interval, limit=50):

    params = {
        "interval": interval
    }

    url = f"{BASE_URL}/{symbol}"

    response = requests.get(
        url,
        params=params,
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
        "open_time": data["time"],
        "open": data["open"],
        "high": data["high"],
        "low": data["low"],
        "close": data["close"],
        "volume": data["vol"]
    })

    # Convert numbers
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

    # Convert timestamp
    df["open_time"] = pd.to_datetime(
        df["open_time"],
        unit="s"
    )

    # Keep latest candles
    df = df.tail(limit).reset_index(drop=True)

    return df


# ==============================
# START TEST
# ==============================

print("🚀 MEXC LTC PERPETUAL TEST STARTING...")
print(f"📌 Symbol: {SYMBOL}")


# 1H
ltc_1h = get_candles(
    SYMBOL,
    "Min60",
    50
)


# 15M
ltc_15m = get_candles(
    SYMBOL,
    "Min15",
    50
)


# ==============================
# DISPLAY DATA
# ==============================

print("\n📊 LTC/USDT PERPETUAL - 1H")
print(
    ltc_1h[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    ].tail(5)
)


print("\n📊 LTC/USDT PERPETUAL - 15M")
print(
    ltc_15m[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    ].tail(5)
)


# ==============================
# CURRENT PRICE
# ==============================

current_price = ltc_15m["close"].iloc[-1]

print(
    f"\n💰 Current LTC Price: ${current_price:.4f}"
)


# ==============================
# DISCORD TEST
# ==============================

if not WEBHOOK_URL:

    raise RuntimeError(
        "DISCORD_WEBHOOK_URL secret not found!"
    )


message = {
    "content": (
        "🟢 **LTC SMC BOT CONNECTION SUCCESS!**\n\n"
        "📡 Exchange: `MEXC Futures`\n"
        "📌 Symbol: `LTC_USDT PERPETUAL`\n"
        f"💰 Current Price: `${current_price:.4f}`\n"
        "⏱ 1H Data: `ONLINE`\n"
        "⏱ 15M Data: `ONLINE`\n"
        "🤖 Discord Webhook: `CONNECTED`\n\n"
        "✅ Ready for SMC Engine"
    )
}


response = requests.post(
    WEBHOOK_URL,
    json=message,
    timeout=10
)


# ==============================
# RESULT
# ==============================

if response.status_code in (200, 204):

    print(
        "\n✅ Discord message sent successfully!"
    )

    print(
        "🎯 MEXC Futures + LTC + Discord = READY"
    )

else:

    print(
        f"\n❌ Discord Error: "
        f"{response.status_code}"
    )

    print(response.text)

    response.raise_for_status()
