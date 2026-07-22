#!/usr/bin/env python3
"""Прочитать конкретные сообщения по id из чата, где userbot состоит участником
(когда Bot API их не отдаёт — напр. bot-to-bot, или история до вступления бота).
Usage: read_msgs.py <chat> <id> [<id> ...]
  <chat> — @username или числовой id (супергруппа/канал со знаком -100…).
Пути креды/сессии/лока переопределяются env USERBOT_ENV / USERBOT_SESSION / USERBOT_LOCK."""
import asyncio, os, sys, fcntl
from telethon import TelegramClient

ROOT = os.path.dirname(os.path.abspath(__file__))
SECRET = os.environ.get("USERBOT_ENV") or os.path.join(ROOT, ".env")
SESSION = os.environ.get("USERBOT_SESSION") or os.path.join(ROOT, "userbot")
LOCK = os.environ.get("USERBOT_LOCK") or "/tmp/userbot_session.lock"

def creds():
    d = {}
    for line in open(SECRET):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return int(d["ARETE_API_ID"]), d["ARETE_API_HASH"]

async def main(chat, ids):
    api_id, api_hash = creds()
    client = TelegramClient(SESSION, api_id, api_hash)
    await client.start()
    chat = int(chat) if chat.lstrip("-").isdigit() else chat
    for m in await client.get_messages(chat, ids=ids):
        if m is None:
            continue
        s = await m.get_sender()
        who = getattr(s, "username", None) or getattr(s, "first_name", "?")
        print(f"=== id {m.id} | {who} | {m.date}")
        print((m.text or "<non-text>"))
        print()
    await client.disconnect()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: read_msgs.py <chat> <id> [<id> ...]")
    chat = sys.argv[1]
    ids = [int(x) for x in sys.argv[2:]]
    lf = open(LOCK, "w"); fcntl.flock(lf, fcntl.LOCK_EX)  # сериализация с send/cron
    asyncio.run(main(chat, ids))
