import os
import requests

webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

if not webhook_url:
    raise RuntimeError("DISCORD_WEBHOOK_URL secret not found!")

payload = {
    "content": "🟢 **SMC Bot Connection Test Successful!**\n"
               "GitHub → Python → Discord connection is working. 🚀"
}

response = requests.post(webhook_url, json=payload, timeout=10)

if response.status_code in (200, 204):
    print("✅ Discord message sent successfully!")
else:
    print(f"❌ Discord error: {response.status_code}")
    print(response.text)
