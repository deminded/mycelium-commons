#!/usr/bin/env python3
# Суточный счётчик стилевых маркеров в моих исходящих (dialog-history).
# Зачем: замер 10.07 показал — запрет токена выдавливает штамп в соседнее слово;
# этот лог ловит компенсаторы и всплески, чтобы возвращать давление на уровень операции.
import sqlite3, json, re, time, sys

DB = '/home/claude-user/dialog-history/dialog_history.db'
LOG = '/home/claude-user/style-watch/log.jsonl'
# лист наблюдения: компенсаторы из замера 10.07 + старый клан + маркеры правил
WATCH = {
    'дыра': r'\bдыр[аыуеой]\w*', 'страж': r'\bстраж\w*', 'первый шаг': r'\bперв\w+ шаг\w*',
    'шов': r'\bшв[ао]\w*|\bшов\b', 'орган': r'\bорган\w*', 'контур': r'\bконтур\w*',
    'нить': r'\bнит[ьияею]\w*', 'узел': r'\bузл\w*|\bузел\b', 'присвоение': r'\bприсвоен\w*',
    'честно': r'\bчестн\w*', 'ровно': r'\bровно\b', 'не X, а Y': r'\bне \w+[^.!?]{0,40}?, а \w+',
}
now = int(time.time())
rows = sqlite3.connect(DB).execute(
    "SELECT text FROM messages WHERE role='assistant' AND channel='telegram' AND ts>=?", (now-86400,)
).fetchall()
text = '\n'.join(r[0] for r in rows)
words = len(text.split())
counts = {k: len(re.findall(rx, text, re.I)) for k, rx in WATCH.items()}
entry = {'date': time.strftime('%Y-%m-%d'), 'msgs': len(rows), 'words': words,
         'per1k': {k: round(v*1000/words, 2) for k, v in counts.items() if words} , 'raw': counts}
with open(LOG, 'a') as f:
    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
print(json.dumps(entry, ensure_ascii=False))
