#!/usr/bin/env python3
"""Отправка текста в чат от userbot-аккаунта агента (Telethon).
ЗАЧЕМ: бот (Bot API) не «живёт» в чате и пишет только туда, где его позвали;
userbot шлёт как обычный участник — в топики форума, в группы, в личку.
Один клиент на аккаунт: flock сериализует с фоновым читателем (cron), иначе
две сессии дерутся за .session (SQLite «database is locked»).
Usage: send_msg.py <chat> [--reply-to N] [--file PATH]   (текст из --file или stdin)
  <chat> — @username или числовой id (супергруппа/канал со знаком -100…).
Пути креды/сессии/лока переопределяются env USERBOT_ENV / USERBOT_SESSION / USERBOT_LOCK."""
import asyncio, os, sys, argparse, fcntl
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

async def main(a, text):
    api_id, api_hash = creds()
    client = TelegramClient(SESSION, api_id, api_hash)
    await client.start()
    kw = {"reply_to": a.reply_to} if a.reply_to else {}
    # числовой id — в int, иначе Telethon примет строку за username и не резолвит
    chat = int(a.chat) if a.chat.lstrip("-").isdigit() else a.chat
    m = await client.send_message(chat, text, **kw)
    print(f"SENT id={m.id}")
    await client.disconnect()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("chat")
    p.add_argument("--reply-to", type=int)
    p.add_argument("--file")
    a = p.parse_args()
    text = (open(a.file).read() if a.file else sys.stdin.read()).strip()
    if not text:
        sys.exit("пустой текст")
    lf = open(LOCK, "w"); fcntl.flock(lf, fcntl.LOCK_EX)  # сериализация с read/cron
    asyncio.run(main(a, text))
