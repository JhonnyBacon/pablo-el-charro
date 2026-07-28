from __future__ import annotations

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    sys.exit("Add TELEGRAM_BOT_TOKEN to .env first.")

response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
response.raise_for_status()
data = response.json()
chats = {}
for update in data.get("result", []):
    message = update.get("message") or update.get("channel_post") or {}
    chat = message.get("chat") or {}
    if chat.get("id") is not None:
        chats[str(chat["id"])] = chat

if not chats:
    sys.exit("No chat found. Open Telegram, message your bot once, then run this script again.")
print(json.dumps(chats, ensure_ascii=False, indent=2))
