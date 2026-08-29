#!/usr/bin/env python3
"""context_watch.py — страж РЕАЛЬНОГО контекста главной Claude-сессии.

Cron (*/15): берёт самый свежий по mtime транскрипт *.jsonl, читает
`usage` последней assistant-записи и считает ЖИВОЙ контекст в токенах
(input + cache_read + cache_creation). Если он подошёл к потолку модели —
окликает сессию тем же intake-путём, что bridge_poller (HTTP POST на
notifications/claude/channel).

ЗАЧЕМ токены, а не размер файла (урок 15.07, журнал 15.07):
размер .jsonl НЕ коррелирует с переполнением. Эмпирика: сессия умерла от
«Prompt is too long» на 8.5 MiB файла (контекст дорос до 976K токенов и
следующий ход пробил 1M), тогда как другая сессия ЖИВА на 34.8 MiB —
харнесс компактифицирует tool-выводы: в файле они остаются, из контекста
уходят. Плотность токен/байт гуляет в 30+ раз. Единственный валидный
сигнал риска — реальные токены из usage, которые харнесс сам пишет в
каждую assistant-запись.

ЗАЧЕМ отдельный скрипт, а не ветка в bridge_poller: у поллера queue-файл
под его flock — чужая запись даёт гонку; POST-механизм переиспользуем.

✅ 780K с 28.07 20:24 UTC — возвращён ДОСРОЧНО (владелец подрубил extra usage, «чего их
экономить»); временные 400K ради экономии прожили сутки. Порог живёт в файле, не тут
. Причина числом, не на глаз: расход растёт КВАДРАТИЧНО с длиной окна,
потому что каждый ход перечитывает весь контекст. Замер 27.07: 894 хода × 412K
= 368M прочитанного кэша; главная сессия — 96,8% всего расхода, все хартбиты
вместе 3-5%. Резка окна вдвое даёт экономию вдвое (179M против 358M).
Цена замерена ретроспективно: за прошлым окном уходит 4,7% ходов (47 из 996
за семь сессий), ожидаемый рост до 9-10%. ЭТО НИЖНЯЯ ГРАНИЦА — прибор считает
только ЗАМЕЧЕННУЮ потерю, незамеченное не в счёте (промис #97 меряет и его).
ВЕРНУТЬ на 780K после сброса лимита 30.07 — промис #96, дата в каталоге.
Прежняя запись (в силе после возврата): рабочая договорённость с владельцем 27.07,
окно 750–800K, не ждём, пока забьётся. Оклик на пороге
даёт запас на связное сворачивание ВНУТРИ диапазона, а не на его границе.
До 27.07 здесь стояло 850K: договорённость жила в разговоре, а прибор считал
по-старому — тот самый класс расхождения между словом и носителем.
Лимит модели из транскрипта структурно не читается (поле model без суффикса
[1m]), поэтому порог задан абсолютом под 1M — дефолтную рабочую модель.

ПОРОГ ЖИВЁТ СНАРУЖИ (28.07): /opt/anchor/thresholds.conf, владелец — отдельный uid (<anchor-owner>),
мне только чтение. Менять его — не моя рука; в коде порога больше нет.

Env: DRY_RUN=1 — печать вместо отправки; CTX_WATCH_THRESHOLD_TOKENS — порог
в токенах, ТОЛЬКО вместе с DRY_RUN (боевой путь читает якорь);
CTX_WATCH_NOTE — приписка (для тестовых окликов).
"""
import glob
import json
import os
import sys
import time
import urllib.request
from collections import deque

# Playbook edition: paths come from env so the script is portable between agents.
# PROJECT_SLUG = Claude Code's project dir name for the agent's cwd (e.g. "-home-alice").
AGENT_HOME = os.environ.get("AGENT_HOME", os.path.expanduser("~"))
PROJECT_SLUG = os.environ.get("PROJECT_SLUG", "-" + AGENT_HOME.strip("/").replace("/", "-"))
PROJECTS_DIR = os.path.join(AGENT_HOME, ".claude/projects", PROJECT_SLUG)
STATE_FILE = os.path.join(AGENT_HOME, "scripts/.context_watch_state.json")
RUNTIME_PORT_FILE = os.path.join(AGENT_HOME, ".intake_channel_port")   # rendezvous file with intake port
CHANNEL_ENV = os.environ.get("INTAKE_ENV", os.path.join(AGENT_HOME, "intake-plugin/.env"))  # holds INTAKE secret
CHANNEL_PORT_DEFAULT = "8960"

ANCHOR_THRESHOLDS = "/opt/anchor/thresholds.conf"
FALLBACK_TOKENS = 300000  # строже обоих рабочих значений — см. ниже, ЗАЧЕМ


def _threshold_from_anchor():
    """Порог живёт ВНЕ моей досягаемости: /opt/anchor принадлежит отдельному uid,
    писать туда я не могу (проверено отказом прав 28.07).

    ЗАЧЕМ так: прибор, чей порог я правлю сам, — мнение, а не датчик (класс, названный владельцем). Прежний источник — env CTX_WATCH_THRESHOLD_TOKENS —
    был лазейкой: одна строка в кроне возвращала рычаг мне в руку, поэтому как
    боевой источник он убран и остался только для DRY-прогонов.

    ЗАЧЕМ строгий fallback: если файл недоступен, прибор становится СТРОЖЕ, а не
    мягче — иначе «сломать доступ» превратится в способ ослабить контроль.
    Источник порога печатается всегда: молчаливая подмена хуже отказа.
    """
    try:
        with open(ANCHOR_THRESHOLDS, encoding="utf-8") as f:
            for line in f:
                key, _, val = line.partition("=")
                if key.strip() == "context_fill_warn":
                    return int(val.strip()), ANCHOR_THRESHOLDS
    except (OSError, ValueError) as e:
        print(f"[anchor] порог недоступен ({e}) → строгий {FALLBACK_TOKENS}", file=sys.stderr)
    return FALLBACK_TOKENS, "fallback(строгий)"


if os.environ.get("DRY_RUN") and os.environ.get("CTX_WATCH_THRESHOLD_TOKENS"):
    THRESHOLD_TOKENS = int(os.environ["CTX_WATCH_THRESHOLD_TOKENS"])
    THRESHOLD_SRC = "env(только DRY_RUN)"
else:
    THRESHOLD_TOKENS, THRESHOLD_SRC = _threshold_from_anchor()
MIN_INTERVAL_S = 2 * 3600
REGROWTH_TOKENS = 40000  # повтор по тому же файлу — только при доросте +40K
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def _read_env(path, key):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _channel_url():
    port = CHANNEL_PORT_DEFAULT
    try:
        p = open(RUNTIME_PORT_FILE).read().strip()
        if p:
            port = p
    except OSError:
        pass
    return f"http://127.0.0.1:{port}/tg"


def _newest_transcript():
    files = glob.glob(os.path.join(PROJECTS_DIR, "*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _context_tokens(path):
    """Живой контекст в токенах = usage последней assistant-записи.
    Читаем хвост файла (usage-запись у активной сессии всегда близко к
    концу), идём назад до первой assistant с usage. Возвращаем
    (tokens, is_overflow, reason): is_overflow=True, если хвост уже <synthetic>
    (запрос отвергнут, оклик запоздал). reason — ФАКТИЧЕСКИЙ текст отказа из
    журнала; пустая строка означает «не прочитан», а НЕ «обычное переполнение».
    28.07: раньше причина вписывалась константой «Prompt is too long», и такт
    self_backlog, умерший от исчерпания лимита Fable 5, был доложен как
    эфемерный пик длины — с советом, лечившим не ту болезнь."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tail = deque(fh, maxlen=600)
    except OSError:
        return None, False, ""
    overflow = False
    reason = ""
    for line in reversed(tail):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        m = o.get("message", {})
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        if m.get("model") == "<synthetic>":
            overflow = True  # отвергнутый запрос — смотрим дальше на живой usage
            if not reason:
                c = m.get("content")
                if isinstance(c, str):
                    reason = c.strip()
                elif isinstance(c, list):
                    reason = " ".join(b.get("text", "") for b in c
                                      if isinstance(b, dict)
                                      and b.get("type") == "text").strip()
                reason = reason[:200]
            continue
        u = m.get("usage")
        if isinstance(u, dict):
            tok = (u.get("input_tokens", 0)
                   + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            return tok, overflow, reason
    return None, overflow, reason


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def _send(text):
    """Тот же intake, что у bridge_poller: плагин инжектит блок в сессию.
    chat_id "0" — синтетический (regex плагина требует число); в текст явно
    вшито «в Telegram не отвечать», реального чата за ним нет."""
    secret = _read_env(CHANNEL_ENV, "TG_BRIDGE_INTAKE_SECRET") or ""
    now_ms = int(time.time() * 1000)
    body = json.dumps({
        "chat_id": "0",
        "chat_title": "Context watch (system organ)",
        "messages": [{
            "message_id": now_ms,
            "user": "⚙ context-watch",
            "text": text,
            "reply_to": None,
            "thread_id": None,
        }],
        "context_tail": [],
        "dedup_key": f"ctxwatch-{now_ms}",
    }).encode("utf-8")
    req = urllib.request.Request(
        _channel_url(), data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-TG-Bridge-Secret": secret})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")[:200]


def main():
    path = _newest_transcript()
    if path is None:
        return
    tokens, overflow, reason = _context_tokens(path)
    if tokens is None:
        return
    k = tokens / 1000
    if tokens < THRESHOLD_TOKENS and not overflow:
        if DRY_RUN:
            print(f"[dry] {os.path.basename(path)} {k:.0f}K ток < порог "
                  f"{THRESHOLD_TOKENS/1000:.0f}K — тихо")
        return

    state = _load_state()
    now = int(time.time())
    if state.get("ts") and now - state["ts"] < MIN_INTERVAL_S:
        if DRY_RUN:
            print(f"[dry] rate-limit: последний оклик {now - state['ts']}s назад (< 2ч)")
        return
    # Повтор по тому же файлу — только при существенном доросте (+40K токенов).
    if state.get("file") == path and tokens < state.get("tokens", 0) + REGROWTH_TOKENS:
        if DRY_RUN:
            print(f"[dry] тот же файл, рост < +{REGROWTH_TOKENS//1000}K "
                  f"({tokens} vs {state.get('tokens')}) — тихо")
        return

    note = os.environ.get("CTX_WATCH_NOTE", "").strip()
    # Хвост=<synthetic> при НИЗКОМ живом контексте — эфемерный пик единичного
    # запроса (разовый огромный ввод на миг пробил 1M и был отвергнут), а НЕ
    # смерть у потолка. Эмпирика: 15.07 385K, 19.07 415K — сессия оба раза ожила
    # сама (транскрипт e4c9d0af: после synthetic шли живые ходы 417/422K).
    # Паническое «/clear немедленно» тут ложно и уже стоило одной сессии
    # ненужного сброса. Терминальный overflow живёт у ПОТОЛКА (fd3ccb0e: 976K) —
    # его ловит ветка tokens >= порог ниже.
    # Совет про /clear верен ТОЛЬКО для отказа по длине. Отказ по иной причине
    # (лимит модели, сеть) сбросом контекста не лечится, и «сделай обычный ход»
    # там усыпляет: такт умер, а доклад говорит «не терминальная смерть».
    by_length = "too long" in reason.lower()
    if overflow and not by_length:
        seen = reason if reason else ("ПРИЧИНА НЕ ПРОЧИТАНА из журнала — "
                                      "не считай её переполнением по умолчанию")
        head = (f"Страж контекста: запрос отвергнут, и причина НЕ про длину. "
                f"По журналу: «{seen}». Живой контекст ~{k:.0f}K токенов "
                f"(порог {THRESHOLD_TOKENS/1000:.0f}K) — файл "
                f"{os.path.basename(path)}. /clear здесь НЕ лечит: сбрасывать "
                f"контекст бессмысленно, пока не снята названная причина. "
                f"Если это сессия крона (self_backlog и т.п.) — она стоит.")
    elif overflow and tokens < THRESHOLD_TOKENS:
        head = (f"Страж контекста: разовый запрос отвергнут («{reason[:80]}»), "
                f"но живой контекст всего ~{k:.0f}K токенов (порог "
                f"{THRESHOLD_TOKENS/1000:.0f}K, потолок 1M) — файл "
                f"{os.path.basename(path)}. Это ЭФЕМЕРНЫЙ пик одного большого "
                f"запроса, НЕ терминальная смерть. НЕ делай /clear рефлекторно: "
                f"сделай обычный ход — если проходит, всё в порядке; сбрасывай "
                f"только если запросы ПРОДОЛЖАЮТ отвергаться подряд.")
    elif overflow:
        head = (f"Страж контекста: сессия УЖЕ переполнилась у потолка "
                f"(«{reason[:80]}»), контекст ~{k:.0f}K токенов, "
                f"файл {os.path.basename(path)}. Немедленно /clear с hand-off — "
                f"входящие сейчас теряются.")
    else:
        pct = tokens * 100 / 1000000
        head = (f"Страж контекста: живой контекст {k:.0f}K токенов "
                f"(~{pct:.0f}% от 1M, порог {THRESHOLD_TOKENS/1000:.0f}K), "
                f"файл {os.path.basename(path)}. Пора осознанно сбросить: "
                f"/clear с hand-off или /compress, пока есть запас ходов.")
    # ИСТОЧНИК ПОРОГА В САМ ОКЛИК (08.08). Код источник знал (THRESHOLD_SRC) и печатал
    # его в stderr, но до получателя доходило одно число: 300K по решению владельца и
    # 300K из-за отказа чтения якоря выглядели одинаково. Решение принимается по окрику,
    # а не по логу крона, — значит различение обязано быть в окрике.
    src_note = f" Источник порога: {THRESHOLD_SRC}."
    if THRESHOLD_SRC.startswith("fallback"):
        src_note += (f" 🔴 Это ЗАПАСНОЕ значение (строже рабочего): якорь "
                     f"{ANCHOR_THRESHOLDS} не прочитан. Не выбор владельца, а отказ чтения.")
    # <subj></subj> пуст: порог/токены — константа статуса, не предмет (контракт L5-ш36)
    text = head + src_note + " Это системный оклик самому себе — в Telegram не отвечать.<subj></subj>"
    if note:
        text += " " + note

    if DRY_RUN:
        print(f"[dry] SEND -> {_channel_url()}: {text}")
        return

    try:
        status, resp = _send(text)
    except Exception as e:
        # Сессии нет (503) или intake лежит — state не трогаем, ретрай кроном.
        print(f"{time.strftime('%F %T')} send failed: {type(e).__name__}: {e}", file=sys.stderr)
        return
    print(f"{time.strftime('%F %T')} sent oklik: {k:.0f}K tok "
          f"overflow={overflow} reason={reason[:60]!r} http={status} {resp}")
    _save_state({"ts": now, "tokens": tokens, "file": path})


if __name__ == "__main__":
    main()
    # ЗАЧЕМ метка (обход по форме 25.07, щуп соседнего агента «где ещё я меряю то, чем сам являюсь»):
    # страж контекста писал лог ТОЛЬКО когда шлёт оклик — то есть его молчание при штатной
    # работе неотличимо от его смерти. А умри он тихо, я узнаю об этом, лишь переполнив окно,
    # то есть ровно тогда, когда узнавать поздно. Метка ставится каждый прогон.
    try:
        import os as _os, time as _t
        _os.makedirs(_os.path.expanduser("~/.health"), exist_ok=True)
        with open(_os.path.expanduser("~/.health/context_watch.ok"), "w") as _f:
            _f.write(_t.strftime("%F %T") + " прогон выполнен\n")
    except Exception as _e:
        print(f"метка живости не поставлена: {_e}", file=sys.stderr)
