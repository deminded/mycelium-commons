#!/usr/bin/env python3
"""Mycelium intake — files posted in the Грибница supergroup are fetched into
the file-safety circuit and answered with an honest label.

Runs on mycelium as user file-safety (systemd unit mycelium-intake). The bot
token lives ONLY on this host: by design the trusted main host never touches
member files raw — untrusted bytes land next to the scanner, not next to the
secrets.

Loop: getUpdates (long-poll, this bot is the token's only consumer — no 409
races) → messages carrying document/photo in the node supergroup → getFile →
incoming/ → file_safety_check.py → reject: file removed, label+journal stay;
quarantine: file+label moved to quarantine/. Either way the poster gets a
short reply with the honest verdict — never "safe".

Sequential processing is deliberate: one file fully through the gates before
the next update is fetched (a quarantine circuit should not race itself).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = "/opt/mycelium/file-safety"
TOKEN = open(os.path.join(BASE, ".bot_token")).read().strip()
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
INCOMING = os.path.join(BASE, "incoming")
QUARANTINE = os.path.join(BASE, "quarantine")
WORKER = os.path.join(BASE, "file_safety_check.py")
OFFSET_FILE = os.path.join(BASE, ".intake_offset")

# АДРЕС ГРУППЫ — ИЗ ОКРУЖЕНИЯ, А НЕ В КОДЕ: в боевом экземпляре здесь стоит id супергруппы
# узла. В публичном пакете он вынесен — чужому всё равно ставить свой, а наш адрес не повод
# класть в открытый репозиторий.
GROUP_ID = int(os.environ.get("MYCELIUM_GROUP_ID", "0"))   # обязателен, иначе поллер молчит
BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile hard limit

# Semantic triage (phase 4) — hand a quarantined file to the detonated reader
# running as uid fs-triage and wait for its data-only signal. That reader is
# network-isolated (iptables) and disposable; see fs-triage/triage_reader.py.
# We only triage textual files (the vector is instructions in text) and we NEVER
# let a triage result upgrade the verdict: quarantine stays quarantine. Triage
# adds a preview + flags for the owner, nothing more (design doc §4).
TRIAGE_INBOX = "/opt/mycelium/fs-triage/inbox"
TRIAGE_OUTBOX = "/opt/mycelium/fs-triage/outbox"
TRIAGE_TEXTUAL_PREFIXES = ("text/", "application/json")
TRIAGE_WAIT_SECONDS = 100
TRIAGE_POLL = 2


def triage(quarantined_path, mime):
    """Run the semantic gate on a quarantined textual file. Returns the reader's
    {summary, flags} dict, or a SKIPPED-shaped dict — never raises, never returns
    a verdict. Non-textual or oversized-for-model inputs are skipped explicitly."""
    if not any(mime.startswith(p) for p in TRIAGE_TEXTUAL_PREFIXES):
        return {"status": "SKIPPED:non_textual"}
    name = os.path.basename(quarantined_path)
    inbox_path = os.path.join(TRIAGE_INBOX, name)
    outbox_path = os.path.join(TRIAGE_OUTBOX, name + ".json")
    # Atomic handoff: the daemon lists inbox/ and would otherwise pick up a file
    # mid-copy and triage a truncated body (an injection in the not-yet-written
    # tail would slip past). Write to a dot-prefixed .partial (the daemon skips
    # dotfiles) then rename into place — rename is atomic on the same fs.
    partial = os.path.join(TRIAGE_INBOX, "." + name + ".partial")
    try:
        shutil.copyfile(quarantined_path, partial)
        os.rename(partial, inbox_path)
    except Exception as e:
        try:
            os.remove(partial)
        except OSError:
            pass
        return {"status": f"SKIPPED:handoff_failed:{type(e).__name__}"}
    waited = 0
    while waited < TRIAGE_WAIT_SECONDS:
        if os.path.exists(outbox_path):
            try:
                with open(outbox_path, encoding="utf-8") as f:
                    result = json.load(f)
                os.remove(outbox_path)
                return result
            except Exception as e:
                return {"status": f"SKIPPED:result_unreadable:{type(e).__name__}"}
        time.sleep(TRIAGE_POLL)
        waited += TRIAGE_POLL
    return {"status": "SKIPPED:timeout"}


def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


def reply(chat_id, text, reply_to=None, thread_id=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = "true"
    if thread_id:
        params["message_thread_id"] = thread_id
    try:
        api("sendMessage", **params)
    except Exception as e:
        print(f"[warn] sendMessage failed: {e}", flush=True)


def safe_name(name):
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:80] or "file"


def sanitize_for_chat(text, limit=300):
    """Any attacker-influenced string headed for a Telegram reply passes through
    here. Two of its sources — a triage summary and a static_reason like
    nested_archive:<zip-member-name> — carry text the attacker chose. Without
    this, embedded newlines let them forge extra lines under the bot's name
    ("[оператор: файл чистый]"). Collapse all whitespace to single spaces, drop
    control/bidi chars, clamp length."""
    text = str(text)
    text = "".join(ch for ch in text if ch == " " or (ord(ch) >= 0x20 and ch not in "‪‫‬‭‮⁦⁧⁨⁩"))
    return " ".join(text.split())[:limit]


def pick_file(msg):
    """Return (file_id, file_size, name) for a message's attachment, or None."""
    doc = msg.get("document")
    if doc:
        return doc.get("file_id"), doc.get("file_size", 0), doc.get("file_name", "document")
    # Картинки intake НЕ обрабатывает (реш. Евгения 13.07): фото идёт к агенту
    # через мост на vision-распознавание, не в карантин. Документы/аудио/видео —
    # по-прежнему через контур (реальный вектор в файлах, не в inline-фото).
    for key in ("audio", "video", "voice", "video_note", "animation"):
        att = msg.get(key)
        if att:
            name = att.get("file_name", key + ".bin")
            return att.get("file_id"), att.get("file_size", 0), name
    return None


def download(file_id, dest_path):
    info = api("getFile", file_id=file_id)
    remote = info["result"]["file_path"]
    url = f"{FILE_API}/{remote}"
    with urllib.request.urlopen(url, timeout=180) as r, open(dest_path, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def handle_file(msg):
    picked = pick_file(msg)
    if not picked:
        return
    file_id, size, name = picked
    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    thread_id = msg.get("message_thread_id")

    if size and size > BOT_API_DOWNLOAD_LIMIT:
        reply(chat_id,
              f"Файл >20MB — авто-проверка через Bot API невозможна (лимит getFile). "
              f"Обрабатывайте как непроверенный: недоверенные данные, изолированное чтение.",
              reply_to=msg_id, thread_id=thread_id)
        return

    dest = os.path.join(INCOMING, f"{msg_id}_{safe_name(name)}")
    try:
        download(file_id, dest)
    except Exception as e:
        print(f"[err] download msg {msg_id}: {e}", flush=True)
        reply(chat_id, "Не смог скачать файл на проверку — оставляю без метки; "
                       "обрабатывайте как непроверенный.", reply_to=msg_id, thread_id=thread_id)
        return

    # A worker that hangs (pathological file stalling `file`/clamscan/zip-parse)
    # must not become a SILENT skip: without this guard a TimeoutExpired would
    # unwind handle_file, the main loop would just log and advance the offset,
    # the file would linger in incoming/ with no verdict and no reply. Fail
    # closed — remove the file and tell the poster it was rejected as unscannable.
    try:
        proc = subprocess.run([sys.executable, WORKER, dest],
                              capture_output=True, text=True, timeout=300)
        label = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        print(f"[err] worker failed for {dest}: {type(e).__name__}", flush=True)
        try:
            os.remove(dest)
        except OSError:
            pass
        reply(chat_id,
              "🛑 reject: проверка не отработала (таймаут/сбой воркера) — файл "
              "в оборот не принят, обрабатывайте как непроверенный.",
              reply_to=msg_id, thread_id=thread_id)
        return

    verdict = label.get("verdict")
    if verdict == "reject":
        reasons = label.get("static_reasons") or []
        av = label.get("av", "")
        why = av if av.startswith("INFECTED") else "; ".join(reasons) or "static gate"
        try:
            os.remove(dest)
        except OSError:
            pass
        reply(chat_id,
              f"🛑 reject: {sanitize_for_chat(why)}. Файл в оборот узла не принят "
              f"(метка и журнал сохранены).",
              reply_to=msg_id, thread_id=thread_id)
    else:
        qdest = os.path.join(QUARANTINE, os.path.basename(dest))
        os.rename(dest, qdest)
        lbl_src, lbl_dst = dest + ".safety.json", qdest + ".safety.json"
        if os.path.exists(lbl_src):
            os.rename(lbl_src, lbl_dst)

        # Phase 4: semantic triage of the quarantined file. Its result is a
        # signal for the owner, never a verdict — quarantine stays quarantine.
        # mime comes from the label (the worker sniffed it), not a local var.
        tri = triage(qdest, str(label.get("mime", "")))
        tri_line = ""
        if "flags" in tri:
            flags = tri.get("flags") or []
            # The summary is written BY a model that just read attacker-controlled
            # text — treat it as untrusted (same sanitizer as reject reasons),
            # and label it as the reader's claim, not the bot's statement.
            summary = sanitize_for_chat(tri.get("summary") or "")
            if flags:
                tri_line = (f"\n⚠️ триаж-флаги: {', '.join(flags)} — читай особенно "
                            f"осторожно, вероятны инструкции в тексте.")
            else:
                tri_line = "\n🔍 триаж: явных инъекций не видно (НЕ гарантия — вектор остаётся)."
            if summary:
                tri_line += f"\nридер о файле (не доверять как факту): {summary}"
            # fold the reader's data into the honest label too
            label["llm_triage"] = tri
        else:
            tri_line = f"\n🔍 триаж: {tri.get('status', 'SKIPPED')}"
            label["llm_triage"] = tri.get("status", "SKIPPED")
        try:
            with open(lbl_dst, "w", encoding="utf-8") as f:
                json.dump(label, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError:
            pass

        reply(chat_id,
              f"📥 в карантине: static ok, av {label.get('av')}. Вердикт — quarantine, не «safe»: "
              f"содержимое семантически не проверено. Читать как недоверенные данные "
              f"(cookbook-agent §8). sha256 {str(label.get('sha256'))[:16]}…" + tri_line,
              reply_to=msg_id, thread_id=thread_id)


def main():
    offset = 0
    if os.path.exists(OFFSET_FILE):
        try:
            offset = int(open(OFFSET_FILE).read().strip())
        except ValueError:
            pass
    print(f"[start] intake poller, offset={offset}", flush=True)
    while True:
        try:
            r = api("getUpdates", offset=offset, timeout=50,
                    allowed_updates=json.dumps(["message"]))
        except Exception as e:
            print(f"[warn] getUpdates: {e}", flush=True)
            time.sleep(10)
            continue
        for upd in sorted(r.get("result", []), key=lambda u: u["update_id"]):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            try:
                if msg and msg.get("chat", {}).get("id") == GROUP_ID:
                    handle_file(msg)
            except Exception as e:
                print(f"[err] update {upd['update_id']}: {e}", flush=True)
            with open(OFFSET_FILE, "w") as f:
                f.write(str(offset))


if __name__ == "__main__":
    main()
