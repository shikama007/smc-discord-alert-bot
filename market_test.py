import requests
import pandas as pd

BASE_URL = "https://api.mexc.com/api/v3/klines"


def get_candles(symbol, interval, limit=10):
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
        "quote_volume",
        "trades",
        "taker_buy_base",
        "taker_buy_quote"
    ]

    df = pd.DataFrame(data, columns=columns)

    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column])

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

    return df


print("🚀 MEXC Market Data Test Starting...")

btc_1h = get_candles("BTCUSDT", "1h", 10)

print("\n📊 BTC/USDT 1H Candles:")
print(btc_1h[["open_time", "open", "high", "low", "close", "volume"]])

print("\n✅ MEXC API connection successful!")
