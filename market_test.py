import requests
import pandas as pd

BASE_URL = "https://api1.binance.com/api/v3/klines"


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
        "taker_buy_quote",
        "ignore"
    ]

    df = pd.DataFrame(data, columns=columns)

    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = df[column].astype(float)

    return df


btc_1h = get_candles("BTCUSDT", "1h")
btc_15m = get_candles("BTCUSDT", "15m")

print("\n===== BTC/USDT 1H =====")
print(btc_1h[["open", "high", "low", "close", "volume"]].tail())

print("\n===== BTC/USDT 15M =====")
print(btc_15m[["open", "high", "low", "close", "volume"]].tail())

print("\n✅ Binance market data connection successful!")
