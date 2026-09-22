import requests

urls = [
    "https://www.google.com",
    "https://api.mexc.com",
    "https://api.binance.com"
]

for url in urls:
    try:
        r = requests.get(url, timeout=10)
        print(url, "=>", r.status_code)
        print(r.text[:200])
    except Exception as e:
        print(url, "=> ERROR:", e)
