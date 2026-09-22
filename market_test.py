import requests
import pandas as pd
import os

BASE_URL = "https://api.mexc.com/api/v3/klines"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


def get_candles(symbol, interval, limit=50):
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    response = requests.get(BASE_URL, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume"
    ]

    df = pd.DataFrame(data, columns=columns)

    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column])

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

    return df


print("🚀 MEXC + Discord Test Starting...")

btc_1h = get_candles("BTCUSDT", "60m", 50)
btc_15m = get_candles("BTCUSDT", "15m", 50)

current_price = btc_15m["close"].iloc[-1]

message = {
    "content": (
        "🚀 **SMC BOT CONNECTION SUCCESS!**\n\n"
        "📡 Exchange: `MEXC`\n"
        "💰 BTC/USDT: `$%.2f`\n"
        "⏱ 1H + 15M Data: `ONLINE`\n"
        "🤖 Discord Webhook: `CONNECTED`"
        % current_price
    )
}

response = requests.post(
    WEBHOOK_URL,
    json=message,
    timeout=10
)

response.raise_for_status()

print("✅ MEXC market data working!")
print("✅ Discord message sent successfully!")
