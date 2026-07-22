#!/usr/bin/env python3
"""Создать форум-топик в супергруппе от userbot-аккаунта (нужны права manage_topics).
ГРАБЛЯ (telethon 1.44): CreateForumTopicRequest живёт в functions.messages, НЕ channels;
аргумент peer=, не channel=.
Usage: create_topic.py <chat_id> "<Заголовок>" [--open FILE]   (--open: открыть топик вводным)
Пути креды/сессии/лока переопределяются env USERBOT_ENV / USERBOT_SESSION / USERBOT_LOCK."""
import asyncio, os, sys, random, argparse, fcntl
from telethon import TelegramClient
from telethon.tl.functions.messages import CreateForumTopicRequest

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

async def main(a):
    api_id, api_hash = creds()
    client = TelegramClient(SESSION, api_id, api_hash)
    async with client:
        r = await client(CreateForumTopicRequest(
            peer=int(a.chat), title=a.title,
            icon_color=0x6FB9F0, random_id=random.randint(1, 2**62)))
        # topic id = сообщение с action MessageActionTopicCreate; фолбэк — первый message в updates
        tid = None
        for u in r.updates:
            m = getattr(u, "message", None)
            if m is not None and getattr(getattr(m, "action", None), "__class__", None) \
               and m.action.__class__.__name__ == "MessageActionTopicCreate":
                tid = m.id
        if tid is None:
            for u in r.updates:
                m = getattr(u, "message", None)
                if m is not None and getattr(m, "id", None):
                    tid = m.id
        print("TOPIC_ID", tid)
        if a.open:
            await client.send_message(int(a.chat), open(a.open, encoding="utf-8").read(), reply_to=tid)
            print("OPENING_SENT")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("chat"); p.add_argument("title"); p.add_argument("--open")
    a = p.parse_args()
    lf = open(LOCK, "w"); fcntl.flock(lf, fcntl.LOCK_EX)  # сериализация с send/read/cron
    asyncio.run(main(a))
