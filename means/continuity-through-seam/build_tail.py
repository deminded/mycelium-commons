#!/usr/bin/env python3
"""Извлечь СЫРОЙ хвост из транскрипта Claude Code (JSONL).
ЗАЧЕМ (#33 непрерывность): свежая сессия дочитывает несжатый хвост прошлой — не только
переваренный дайджест, — возвращая текстуру недавнего (человеческий градиент «чем свежее,
тем подробнее помнишь»). Хвост живёт ФАЙЛОМ, т.к. инлайн потолок additionalContext = 10k.
"""
import json, argparse

CTX_START = "— недавний контекст чата"
CTX_END = "Ответить: reply("

def strip_chat_context(s):
    """Свернуть блоки «недавний контекст чата» из входящих <channel>.

    ЗАЧЕМ: замер 27.07 — 64% символов хвоста (1092 строки из 1704) занимали эти блоки, одна и
    та же спека вошла в файл 6 раз. Бюджет max-chars уходил на дубли того, что лежит выше в том
    же файле, и окно хвоста сжималось до получаса. Факт свёртки печатается строкой: обрыв, о
    котором не сообщено, — молчаливое усечение, а оно хуже потери.
    """
    if CTX_START not in s:
        return s
    out, skip, cut = [], False, 0
    for line in s.split("\n"):
        if CTX_START in line:
            skip, cut = True, 0
        if skip:
            cut += 1
            if line.startswith(CTX_END):
                out.append(f"[свёрнут блок «недавний контекст чата»: {cut} строк]")
                out.append(line)
                skip = False
            continue
        out.append(line)
    if skip:
        out.append(f"[свёрнут блок «недавний контекст чата»: {cut} строк, хвост блока без конца]")
    return "\n".join(out)


def blocks_text(msg):
    """Читаемый текст из content-блоков одного message (text/thinking/tool кратко)."""
    c = msg.get("content")
    if isinstance(c, str):
        return strip_chat_context(c)
    out = []
    for b in (c or []):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append(b.get("text", ""))
        elif t == "thinking":
            out.append("[мысль] " + (b.get("thinking") or b.get("text") or ""))
        elif t == "tool_use":
            out.append(f"[tool_use {b.get('name','')}] "
                       + json.dumps(b.get("input", {}), ensure_ascii=False)[:300])
        elif t == "tool_result":
            content = b.get("content")
            s = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            out.append("[tool_result] " + (s or "")[:300])
    return strip_chat_context("\n".join(x for x in out if x))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--max-chars", type=int, default=120000)
    a = ap.parse_args()

    rows = []
    for line in open(a.transcript, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") in ("user", "assistant"):
            rows.append(r)

    # Идём с конца, набираем реплики, пока не упрёмся в бюджет символов.
    chunks, total = [], 0
    for r in reversed(rows):
        m = r.get("message", {})
        role = m.get("role", r.get("type"))
        txt = blocks_text(m)
        if not txt.strip():
            continue
        block = f"### {role}\n{txt}\n"
        total += len(block)
        chunks.append(block)
        if total >= a.max_chars:
            break
    chunks.reverse()

    header = (f"# СЫРОЙ ХВОСТ прошлой сессии — последние {len(chunks)} реплик (~{total} симв)\n"
              f"# Источник: {a.transcript}\n"
              f"# Это несжатое сырьё: перечитывай по нужде, дайджест его не заменяет.\n\n")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(chunks))
    print(a.out)

if __name__ == "__main__":
    main()
