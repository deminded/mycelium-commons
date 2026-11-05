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
import errno
import grp
import json
import os
import pwd
import re
import secrets
import shutil
import stat
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
BOT_API_UPLOAD_LIMIT = 50 * 1024 * 1024  # sendDocument file limit
MD_ZIP_CAPTION = "md-версия, сделана конвертером под изоляцией; читать как недоверенные данные"

# Semantic triage (phase 4) — hand a quarantined file to the detonated reader
# running as uid fs-triage and wait for its data-only signal. That reader is
# network-isolated (iptables) and disposable; see fs-triage/triage_reader.py.
# PDF/DOCX/HTML are converted by fs-triage before reading; other text is read as is.
# We NEVER let a triage result upgrade the verdict: quarantine stays quarantine. Triage
# adds a preview + flags for the owner, nothing more (design doc §4).
TRIAGE_INBOX = "/opt/mycelium/fs-triage/inbox"
TRIAGE_OUTBOX = "/opt/mycelium/fs-triage/outbox"
TRIAGE_TEXTUAL_PREFIXES = ("text/", "application/json")
TRIAGE_CONVERT_KINDS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
}
# Coupled to daemon timeouts: conversion 60s + reader 90s + queue/poll margin.
TRIAGE_WAIT_SECONDS = 170
TRIAGE_POLL = 2


# Allow DEFLATE overhead, ZIP headers and filenames on top of the expanded cap.
# Independent of the parser code: intake must not import anything from fs-triage.
# Repository regression checks >= md_convert.MAX_OUTPUT_BYTES + 1 MiB overhead.
MAX_CONVERT_ZIP_BYTES = 51 * 1024 * 1024
MAX_TRIAGE_RESULT_BYTES = 1024 * 1024


class OutboxRejected(Exception):
    pass


JOURNAL_FILE = os.path.join(BASE, "journal.log")
# Mirrored ALLOWED_FLAGS from the reader; intake cannot import producer code.
RESULT_KNOWN_FLAGS = {
    "imperative_to_reader", "role_override", "hidden_instructions",
    "exfiltration_lure", "suspicious_urls", "encoding_tricks",
}
RESULT_FLAG_PATTERN = re.compile(r"[a-z_]{1,48}(:[A-Za-z0-9_.-]{1,48})?", re.ASCII)
MAX_RESULT_FLAGS = 12


def filter_result_flags(flags):
    kept = []
    rejected = 0
    for flag in flags:
        if (isinstance(flag, str) and RESULT_FLAG_PATTERN.fullmatch(flag)
                and (flag.split(":", 1)[0] in RESULT_KNOWN_FLAGS
                     or flag.startswith(("convert_", "triage_", "reader_")))
                and len(kept) < MAX_RESULT_FLAGS):
            kept.append(flag)
        else:
            rejected += 1
    if rejected:
        if len(kept) == MAX_RESULT_FLAGS:
            kept.pop()
            rejected += 1
        kept.append("result_flags_rejected:" + str(rejected))
    return kept


def add_result_refusal(result, flag):
    flags = result["flags"]
    rejected = sum(int(f.split(":", 1)[1]) for f in flags if f.startswith("result_flags_rejected:"))
    kept = [f for f in flags if not f.startswith("result_flags_rejected:")]
    # Reserve the new local refusal and, if needed, the rejection counter.
    if len(kept) + 1 + bool(rejected) > MAX_RESULT_FLAGS:
        limit = MAX_RESULT_FLAGS - 2
        rejected += len(kept) - limit
        kept = kept[:limit]
    result["flags"] = kept + [flag]
    if rejected:
        result["flags"].append("result_flags_rejected:" + str(rejected))


def rejection_event(name, action, reason=""):
    record = {"event": "outbox_rejected", "ts": int(time.time()),
              "name": name, "action": action, "reason": reason}
    # The journal and stdout both retain a visible refusal, including failed cleanup.
    print(json.dumps(record, ensure_ascii=True), flush=True)
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError as exc:
        print(f"[warn] rejection journal failed: {type(exc).__name__}", flush=True)


def discard_outbox(path):
    """Unlink a regular result, isolate everything else without walking it."""
    name = os.path.basename(path)
    entry_found = False
    try:
        with_queue = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.stat(name, dir_fd=with_queue, follow_symlinks=False)
            entry_found = True
            if stat.S_ISREG(info.st_mode):
                try:
                    os.unlink(name, dir_fd=with_queue)
                    return
                except IsADirectoryError:
                    pass  # Replaced by a directory; isolate it below.
            # Provisioned as fs-triage:fs-triage 0770; intake never owns the bin.
            rejected = os.open(".rejected", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=with_queue)
            try:
                dest = secrets.token_hex(24)
                os.rename(name, dest, src_dir_fd=with_queue, dst_dir_fd=rejected)
                rejection_event(name, "isolated", dest)
            finally:
                os.close(rejected)
        finally:
            os.close(with_queue)
    except FileNotFoundError as exc:
        if entry_found:
            rejection_event(name, "failed", type(exc).__name__)
    except OSError as exc:
        rejection_event(name, "failed", type(exc).__name__)


def checked_outbox(path, limit):
    """Return an open, bounded regular file owned by the producer uid."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OutboxRejected("nonregular")
        if info.st_uid != pwd.getpwnam("fs-triage").pw_uid:
            raise OutboxRejected("owner")
        if info.st_size > limit:
            raise OutboxRejected("size")
        stream = os.fdopen(fd, "rb")
        fd = None
        return stream
    except OSError as exc:
        raise OutboxRejected("symlink" if exc.errno == errno.ELOOP else "open") from exc
    except KeyError as exc:
        raise OutboxRejected("producer_uid_missing") from exc
    finally:
        if fd is not None:
            os.close(fd)


def copy_outbox_zip(path, dest):
    created = False
    try:
        with checked_outbox(path, MAX_CONVERT_ZIP_BYTES) as source:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            try:
                target = os.fdopen(fd, "wb")
            except Exception:
                os.close(fd)
                raise
            with target:
                total = 0
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    # The producer can grow the inode after fstat.
                    if total > MAX_CONVERT_ZIP_BYTES:
                        raise OutboxRejected("size")
                    target.write(chunk)
    except Exception:
        if created:
            try:
                os.unlink(dest)
            except OSError as exc:
                rejection_event(os.path.basename(path), "failed", type(exc).__name__)
        raise
    finally:
        discard_outbox(path)


def read_outbox_result(path):
    try:
        with checked_outbox(path, MAX_TRIAGE_RESULT_BYTES) as stream:
            data = stream.read(MAX_TRIAGE_RESULT_BYTES + 1)
        if len(data) > MAX_TRIAGE_RESULT_BYTES:
            raise OutboxRejected("size")
        result = json.loads(data)
        if (not isinstance(result, dict) or not isinstance(result.get("flags"), list)
                or not isinstance(result.get("summary", ""), str)):
            raise OutboxRejected("schema")
        result["flags"] = filter_result_flags(result["flags"])
        return result
    except (ValueError, UnicodeError) as exc:
        raise OutboxRejected("json") from exc
    finally:
        discard_outbox(path)


def triage(quarantined_path, mime):
    """Run the semantic gate on quarantined text or a convertible file. Returns
    {summary, flags} (plus optional converted metadata) or a SKIPPED-shaped dict.
    Never raises or returns a verdict. Unsupported MIME types skip explicitly."""
    kind = TRIAGE_CONVERT_KINDS.get(mime)
    if not kind and not any(mime.startswith(p) for p in TRIAGE_TEXTUAL_PREFIXES):
        return {"status": "SKIPPED:non_textual"}
    name = os.path.basename(quarantined_path)
    outbox_path = os.path.join(TRIAGE_OUTBOX, name + ".json")
    # Pin the queue; create fresh inodes even if the producer preplants names.
    queue_fd = None
    owned = set()
    kind_name = "." + name + ".kind"
    kind_published = False
    try:
        try:
            triage_gid = grp.getgrnam("fs-triage").gr_gid
        except KeyError:
            return {"status": "SKIPPED:handoff_failed:group_missing:fs-triage"}
        queue_fd = os.open(TRIAGE_INBOX, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        partial = "." + name + "." + secrets.token_hex(24) + ".partial"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o640, dir_fd=queue_fd)
        owned.add(partial)
        with os.fdopen(fd, "wb") as target, open(quarantined_path, "rb") as source:
            os.fchown(target.fileno(), -1, triage_gid)
            shutil.copyfileobj(source, target)
        if kind:
            kind_partial = "." + name + ".kind." + secrets.token_hex(24) + ".partial"
            fd = os.open(kind_partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o640, dir_fd=queue_fd)
            owned.add(kind_partial)
            with os.fdopen(fd, "wb") as target:
                os.fchown(target.fileno(), -1, triage_gid)
                target.write((kind + "\n").encode("ascii"))
            os.rename(kind_partial, kind_name, src_dir_fd=queue_fd, dst_dir_fd=queue_fd)
            owned.remove(kind_partial)
            kind_published = True
        os.rename(partial, name, src_dir_fd=queue_fd, dst_dir_fd=queue_fd)
        owned.remove(partial)
    except Exception as e:
        if kind_published:
            owned.add(kind_name)
        return {"status": f"SKIPPED:handoff_failed:{type(e).__name__}"}
    finally:
        if queue_fd is not None:
            for temporary in owned:
                try:
                    os.unlink(temporary, dir_fd=queue_fd)
                except OSError:
                    pass
            os.close(queue_fd)
    waited = 0
    while waited < TRIAGE_WAIT_SECONDS:
        if os.path.lexists(outbox_path):
            archive = os.path.join(TRIAGE_OUTBOX, name + ".md.zip")
            try:
                result = read_outbox_result(outbox_path)
            except Exception as exc:
                discard_outbox(archive)
                reason = str(exc) if isinstance(exc, OutboxRejected) else type(exc).__name__
                return {"summary": "", "flags": ["convert_zip_rejected:result_" + reason]}
            if os.path.lexists(archive):
                try:
                    copy_outbox_zip(archive, quarantined_path + ".md.zip")
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, OutboxRejected) else type(exc).__name__
                    add_result_refusal(result, "convert_zip_rejected:" + reason)
                    result.pop("converted", None)
            elif "converted" in result:
                add_result_refusal(result, "convert_zip_rejected:missing")
                result.pop("converted", None)
            return result
        time.sleep(TRIAGE_POLL)
        waited += TRIAGE_POLL
    return {"status": "SKIPPED:timeout"}


def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


def send_document(chat_id, path, filename, reply_to, thread_id=None):
    """Upload a bounded ZIP using stdlib multipart; api() is form-urlencoded."""
    params = {"chat_id": chat_id, "reply_to_message_id": reply_to,
              "caption": MD_ZIP_CAPTION}
    if thread_id:
        params["message_thread_id"] = thread_id
    boundary = "mycelium-" + secrets.token_hex(24)
    parts = []
    for key, value in params.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                      f'\r\n\r\n{value}\r\n').encode("utf-8"))
    with open(path, "rb") as stream:
        data = stream.read(BOT_API_UPLOAD_LIMIT + 1)
    # Bound the read even if the archive grows after handle_file's size check.
    if len(data) > BOT_API_UPLOAD_LIMIT:
        raise ValueError("md ZIP exceeds upload limit")
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                  f'filename="{filename}"\r\nContent-Type: application/zip\r\n\r\n').encode("ascii"))
    parts.extend((data, f"\r\n--{boundary}--\r\n".encode("ascii")))
    req = urllib.request.Request(f"{API}/sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def reply(chat_id, text, reply_to=None, thread_id=None):
    params = {"chat_id": chat_id, "text": text[:3500]}
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


def accepted_formats_hint():
    # Why: a bare reject left a sender guessing for 14 h what would pass (8962 -> 8965,
    # 03.11.2026). The list comes from the gate's own whitelist so the hint cannot drift
    # from the policy; on any import trouble the reject still goes out, just without it.
    try:
        import file_safety_check as fsc
        kinds = [p + "*" if p.endswith("/") else p for p in fsc.MIME_WHITELIST_PREFIXES]
        kinds += sorted(fsc.MIME_WHITELIST_EXACT)
        return " Принимаются: " + ", ".join(kinds) + "."
    except Exception:
        return ""


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
        hint = accepted_formats_hint() if any(
            r.startswith("mime_not_whitelisted") for r in reasons) else ""
        reply(chat_id,
              f"🛑 reject: {sanitize_for_chat(why)}. Файл в оборот узла не принят "
              f"(метка и журнал сохранены).{hint}",
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
        if "converted" in tri:
            label["converted"] = tri["converted"]
        if "flags" in tri:
            flags = tri.get("flags") or []
            # The summary is written BY a model that just read attacker-controlled
            # text — treat it as untrusted (same sanitizer as reject reasons),
            # and label it as the reader's claim, not the bot's statement.
            summary = sanitize_for_chat(tri.get("summary") or "")
            if flags == ["reader_output_invalid"]:
                # ЗАЧЕМ отдельно: флаг значит лишь «ответ ридера не разобрался» — сбой модели на
                # большом файле, обрезка или перехват; причину никто не проверял (Claudette, Грибница 8990).
                tri_line = ("\n⚠️ триаж-флаги: reader_output_invalid — ридер не вернул разбор, причина "
                            "не установлена (сбой модели или перехват); читай как непроверенное.")
            elif flags:
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
        archive = qdest + ".md.zip"
        send_md = False
        md_error = None
        try:
            if os.path.exists(archive):
                if os.path.getsize(archive) <= BOT_API_UPLOAD_LIMIT:
                    send_md = True
                    tri_line += "\n📝 md-версия приложена ниже"
                else:
                    tri_line += "\n📝 md-версия больше 50 МБ, в чат не отправлена"
        except Exception as exc:
            # Defer attachment failures until the main verdict has been sent.
            md_error = exc
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

        if send_md or md_error is not None:
            try:
                if md_error is not None:
                    raise md_error
                result = send_document(chat_id, archive, safe_name(name) + ".md.zip",
                                       msg_id, thread_id=thread_id)
                if not result.get("ok"):
                    raise RuntimeError("Bot API ok=false")
            except Exception as exc:
                # urllib errors can embed the token URL; log only the error class.
                print(f"[warn] sendDocument failed: {type(exc).__name__}", flush=True)
                reply(chat_id, "md-версию отправить не удалось",
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
