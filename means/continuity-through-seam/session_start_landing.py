#!/usr/bin/env python3
"""Локальный SessionStart-хук #33 (ПРИЗЕМЛЕНИЕ). Курирует ≤10k дайджест ИНЛАЙН
(директива + верхний блок хэнд-оффа + открытые обязательства тела + индекс рефлексий)
и дописывает путь к файлу СЫРОГО хвоста прошлой сессии.

ЗАЧЕМ: JSON additionalContext инъектится инлайн до 10k (проверено #33-А), тогда как
plain-stdout режется харнессом до 2KB-превью → нынешний общий хук терял хэнд-офф молча
(урок 15.07). Тут дайджест влезает целиком + свежий хвост доступен дословно файлом.

ИЗОЛЯЦИЯ: локальный (общий /opt/shared/ не трогаем — остаётся соседним агентам).
Пути через env (CC_HOME/CC_PROJ_DIR/CC_CONT_DIR/CC_DIGEST/REFLECTIONS_DIR) → гоняется в
фейковом дереве. ФЕЙЛСЕЙФ: любой сбой всё равно отдаёт валидный JSON с директивой +
хэнд-оффом (не пусто) — молчаливый разрыв непрерывности недопустим.
"""
import json, sys, os, glob, re, subprocess, datetime

CEIL = 8000    # ЗАПАС, не потолок. Замер 01.08 (L7-ш45, 213 пар, 0 ошибок): порог
               # транспорта — РОВНО 10000 кодовых единиц UTF-16 (JS .length у режущего),
               # НЕ ~9983 символа, как считал замер 31.07. Расхождение мерок = число
               # не-BMP эмодзи (🔴🧠… по +1 единице); len() ниже считает кодовые ТОЧКИ,
               # поэтому целиться в 10000 этой мерой значит уезжать за край на украшения.
               # Кто поднимет CEIL к границе — зажимать по len(s.encode('utf-16-le'))//2.
KEEP = 10      # сколько tail-файлов истории держать
HERE = os.path.dirname(os.path.abspath(__file__))

DIRECTIVE = (
    "=== ПРОДОЛЖЕНИЕ ПОСЛЕ СБРОСА (#33: дайджест инлайн ≤10k + сырой хвост файлом) ===\n"
    "Ты — свежая сессия после self-/clear. Индекс памяти (MEMORY.md) уже в системном "
    "контексте; полные файлы — memory/*.md (Read/ассоциатор). Ниже: верхний блок хэнд-оффа "
    "[переварено] + открытые обязательства тела + индекс рефлексий + путь к СЫРОМУ хвосту "
    "прошлой сессии. Подхвати открытые нити, прежде чем ждать промпта.\n"
)

def find_prior_transcript(cur_session_id, proj_dir):
    """Прошлый транскрипт = самый свежий топ-левел *.jsonl, кроме текущего и суб-агентов."""
    files = [f for f in glob.glob(os.path.join(proj_dir, "*.jsonl"))
             if "/subagents/" not in f
             and (not cur_session_id or cur_session_id not in os.path.basename(f))]
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0] if files else None

def prune_tails(cont_dir, keep=KEEP):
    tails = sorted(glob.glob(os.path.join(cont_dir, "tail-*.txt")),
                   key=os.path.getmtime, reverse=True)
    for old in tails[keep:]:
        try: os.remove(old)
        except OSError: pass

def top_block(text):
    """Верхний (свежий) блок хэнд-оффа: пропустить YAML-фронтматтер, затем до первого
    разделителя '---' на своей строке (memory-файл открывается '--- ... ---' фронтматтером)."""
    lines = text.splitlines(keepends=True)
    i = 0
    if lines and lines[0].strip() == "---":     # пропуск фронтматтера
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1                                  # за закрывающий ---
    out = []
    while i < len(lines) and lines[i].strip() != "---":
        out.append(lines[i]); i += 1
    return "".join(out)

def body_ledger(home):
    """Компактный ledger: только '▸'-заголовки открытых спанов (без многострочных деталей)."""
    script = os.path.join(home, "body", "session_context.py")
    if not os.access(script, os.X_OK):
        return ""
    try:
        r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=5)
        heads = [ln.rstrip() for ln in r.stdout.splitlines() if ln.lstrip().startswith("▸")]
        if not heads:
            return ""
        return ("\n\n=== ОТКРЫТЫЕ ОБЯЗАТЕЛЬСТВА (тело, заголовки; полное — "
                "python3 ~/body/session_context.py) ===\n" + "\n".join(heads))
    except Exception:
        return ""

def promise_due_block(home):
    """Что горит по срокам — ИЗ ПЕРВОИСТОЧНИКА, а не из переписанного руками списка.

    ЗАЧЕМ (заимствованная идея «единая проекция состояния», #440; случай 15.08):
    вход давал смене ДВИЖЕНИЕ обещаний за сутки, но не горящие сроки — их несла только
    передача, писанная рукой уходящей смены. В тот день передача уверяла, что #428 горит
    16.08, а в promises.jsonl стоит 20.08. Смена верит тому, что видит первым.
    Логика счёта НЕ дублируется: зовётся тот же promise.py, что и в ручном разборе, —
    иначе завёлся бы второй прибор, который однажды разойдётся с первым.
    """
    script = os.path.join(home, "promises", "promise.py")
    if not os.path.exists(script):
        return ""
    try:
        r = subprocess.run([sys.executable, script, "list", "--due", "--brief", "--horizon", "3"],
                           capture_output=True, text=True, timeout=15, cwd=os.path.dirname(script))
        out = (r.stdout or "").strip()
        if not out:
            return ""
        return ("\n\n=== ЧТО ГОРИТ ПО СРОКАМ (promises.jsonl, первоисточник; полное — "
                "python3 ~/promises/promise.py list --due) ===\n" + out)
    except Exception:
        return ""

def reflections_index(home, budget):
    if budget < 120:
        return ""
    rdir = os.environ.get("REFLECTIONS_DIR",
                          f"${VAULT_DIR:-/nonexistent}/Reflections-{os.path.basename(home)}")
    if not os.path.isdir(rdir):
        return ""
    files = sorted(glob.glob(os.path.join(rdir, "*.md")), key=os.path.getmtime, reverse=True)[:15]
    head = "\n\n=== ИНДЕКС РЕФЛЕКСИЙ (свежие; читать по нужде / vault-memory MCP) ===\n"
    body = ""
    for f in files:
        name = os.path.basename(f)
        title = ""
        try:
            for ln in open(f, encoding="utf-8"):
                if ln.startswith("title:"):
                    title = ln.split("title:", 1)[1].strip(); break
        except OSError:
            pass
        row = (f"- {name} — {title}".rstrip() if title else f"- {name}") + "\n"
        if len(head) + len(body) + len(row) > budget:
            body += "…\n"; break
        body += row
    return head + body if body else ""

DELTA_SKIP = {"MEMORY.md", "project_handoff_current.md"}
MD_HEADING = re.compile(r"#{1,6}\s+\S")  # заголовок, а не «#543 …» в тексте

def promise_delta(hours=24):
    """Что прибавилось и закрылось в КАТАЛОГЕ ОБЕЩАНИЙ за сутки.

    ЗАЧЕМ: слой суток 27.07 собран по git-диффу memory/ — и оказался слеп к ~/promises.
    Проверено на живом случае в тот же час: промис #106 «вернуться к картинке ПЕРВЫМ ДЕЛОМ»
    заведён после переписывания хэнд-оффа, в память не попадал, и его поднял только сырой
    хвост последними строками. То есть самое срочное дело лежало в единственном носителе,
    которого слой суток не видел.
    ПРЕДЕЛ (объявлен до чисел): читаются события журнала за окно; правки текста промиса
    задним числом здесь не видны, как и в memory-диффе.

    ЧТО СЧИТАЕТСЯ ЗАВЕДЁННЫМ (правка 19.08, поймано на себе): пункт, чей id ВПЕРВЫЕ
    появился в окне, — а не всякая строка журнала. Прежде сюда шла и каждая дописка
    `due --note` к старому пункту: 19.08 счёт вышел 138 против настоящего 21. Хуже самой
    ошибки была асимметрия единиц: закрытие бывает ровно раз на пункт, поэтому «закрыто»
    считалось верно, и пропорция читалась как лавина 13.8:1 вместо настоящей 2.1:1 —
    входящая смена видела это число первым и не имела повода его перепроверить.
    Дописи не выброшены, они идут отдельным числом: их рост — свой сигнал, но не заведение.
    """
    home = os.environ.get("CC_HOME", os.path.expanduser("~"))
    path = os.environ.get("CC_PROMISES", os.path.join(home, "promises", "promises.jsonl"))
    born, closed, notes = [], [], 0
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
        seen_before, fresh = set(), []
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            try:
                ts = datetime.datetime.strptime(r.get("ts", ""), "%Y-%m-%d %H:%M")
            except Exception:
                ts = None
            # НЕРАЗОБРАННОЕ ВРЕМЯ СЧИТАЕМ СТАРЫМ (ts числом — записи первых дней): ошибка
            # такого допущения занижает «заведено», а не завышает.
            if ts is None or ts < cutoff:
                seen_before.add(r.get("id"))
                continue
            fresh.append(r)
        for r in fresh:
            i = r.get("id")
            row = f"#{i} {(r.get('what') or '')[:150]}"
            if r.get("status") == "done":
                closed.append(row)
            elif i in seen_before:
                notes += 1
            else:
                born.append(row)
                seen_before.add(i)
    except OSError:
        return "", 0, 0, 0
    if not born and not closed and not notes:
        return "", 0, 0, 0
    out = ["\n## КАТАЛОГ ОБЕЩАНИЙ за сутки (~/promises)\n"]
    if born:
        out.append(f"### заведено ({len(born)}) — НОВЫЕ пункты, а не строки журнала\n")
        out += [f"  ◻ {r}\n" for r in born]
    if notes:
        out.append(f"### дописей к прежним пунктам: {notes} — это не заведение; "
                   "сам текст лежит в пунктах (promise.py list)\n")
    if closed:
        out.append(f"### закрыто ({len(closed)})\n")
        out += [f"  ✓ {r}\n" for r in closed]
    return "".join(out), len(born), len(closed), notes

def memory_delta(proj_dir, cont_dir, budget, hours=24):
    """Что ПРИБАВИЛОСЬ в память за сутки — по git-диффу, а не по факту «файл тронут».

    ЗАЧЕМ: хэнд-офф пишется в жанре «состояние» — туда попадает открытое и требующее
    действия. А полученное за день (чужой ход, новая архитектура, свой интерес) открытой
    линией не является и не попадает никуда. Замер #99 (27.07, пункты дал внешний): две записи лежали в памяти, записанные за час до
    шва, и поднялись НОЛЁМ — индекс адресует файл, а не свежий слой в нём.
    ПРЕДЕЛ ПРИБОРА (объявлен до чисел): видны только ДОБАВЛЕННЫЕ строки; правки-удаления
    и изменения файлов вне git не показываются.
    """
    prom_text, n_born, n_closed, n_notes = promise_delta(hours)
    mem = os.path.join(proj_dir, "memory")
    # рядом с хвостами, а не в каталоге скрипта: путь должен изолироваться тем же
    # CC_CONT_DIR — иначе тест в песочнице пишет в боевой файл (поймано при написании теста)
    delta_path = os.path.join(cont_dir, "memory-delta.txt")
    if budget < 300:
        return ""
    # git памяти может не быть вовсе — каталог обещаний живёт отдельным носителем и должен
    # доехать самостоятельно (иначе повторится 27.07: срочное лежало там, где слой не смотрел)
    base, diff = "", ""
    if os.path.isdir(os.path.join(mem, ".git")):
        try:
            base = subprocess.run(["git", "-C", mem, "rev-list", "-1",
                                   f"--before={hours} hours ago", "HEAD"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
            # diff до РАБОЧЕГО ДЕРЕВА, а не до HEAD: ловит и закоммиченное, и ещё не закоммиченное
            if base:
                diff = subprocess.run(["git", "-C", mem, "diff", "--unified=0", base],
                                      capture_output=True, text=True, timeout=10).stdout
        except Exception:
            base, diff = "", ""

    added, cur = {}, None
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            path = ln[6:]
            cur = None if (path.startswith("archive/") or ".bak" in path
                           or os.path.basename(path) in DELTA_SKIP) else path
            continue
        # берём только ЗАГОЛОВКИ добавленных разделов: их немного, и в моей разметке они
        # написаны содержанием («ассоциация как МОДУЛЯТОР, а не источник»), а не адресом.
        # РЕШЁТКА + ПРОБЕЛ, а не просто решётка (правка 19.08): ссылка на пункт в тексте
        # («#543 (прислал ли собеседник данные)…») проходила за раздел. Цена мала — 1 из 88 за
        # сутки, — но читатель принимал обрывок фразы за заголовок; поймано на себе.
        if cur and ln.startswith("+") and not ln.startswith("+++") and MD_HEADING.match(ln[1:].strip()):
            added.setdefault(cur, []).append(ln[1:].strip().lstrip("# "))
    added = {k: v for k, v in added.items() if v}
    if not added and not prom_text:
        return ""

    n_sec = sum(len(v) for v in added.values())
    order = sorted(added, key=lambda p: os.path.getmtime(os.path.join(mem, p))
                   if os.path.exists(os.path.join(mem, p)) else 0, reverse=True)
    # ПОЛНОЕ — файлом, ОБРЕЗОК — инлайн: та же развязка, что у сырого хвоста. Выбирать
    # «главный» раздел из десяти, дописанных за день в одну карточку, прибор не может —
    # любая эвристика тут льстит (проверено дважды на этом же дне: и первый, и последний
    # добавленный раздел прошли мимо той записи, которая и был потерян при замере #99).
    try:
        with open(delta_path, "w", encoding="utf-8") as f:
            f.write(f"# ПАМЯТЬ: ЧТО ПРИБАВИЛОСЬ ЗА СУТКИ — {n_sec} разделов в {len(added)} карточках\n"
                    f"# git diff {base[:8]}..рабочее дерево. ПРЕДЕЛ: только добавленные ЗАГОЛОВКИ;\n"
                    f"# правки внутри разделов и удаления не видны. Карточки — в {mem}/\n")
            for path in order:
                f.write(f"\n## {os.path.basename(path)} ({len(added[path])})\n")
                for h in added[path]:
                    f.write(f"  · {h}\n")
            if prom_text:
                f.write(prom_text)
    except OSError:
        return ""

    head = ("\n\n=== ПАМЯТЬ: ЧТО ПРИБАВИЛОСЬ ЗА СУТКИ ===\n"
            f"🔴 Read ЦЕЛИКОМ: {delta_path} — {n_sec} новых разделов в {len(added)} карточках.\n"
            "Индекс их НЕ доставляет: он описывает карточку вообще, а не сегодняшний слой в ней "
            "(замер #99: две свежие карточки дали ноль, лёжа в памяти час). Что зацепило — Read карточки.\n"
            + (f"В том же файле — КАТАЛОГ ОБЕЩАНИЙ за сутки: заведено {n_born}, закрыто {n_closed}"
               f" (дописей к прежним пунктам {n_notes} — они не заведение) "
               "(память его не видит, а срочное живёт там).\n" if prom_text else "")
            + "Свежайшие карточки дня:\n")
    rows, shown = "", 0
    for path in order:
        row = f"- {os.path.basename(path)} ({len(added[path])}): {added[path][-1]}"
        row = (row[:150] + "…") if len(row) > 150 else row
        if len(head) + len(rows) + len(row) + 60 > budget:
            break
        rows += row + "\n"; shown += 1
    if shown < len(added):
        rows += f"[инлайн показано {shown} из {len(added)} — полнота по Read выше]\n"
    return head + rows

def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    session_id = payload.get("session_id", "")

    # 🔴 НЕ ПРИЗЕМЛЯТЬ ПРОБУ (12.08 18:52, оплачено переписанной передачей). Проба здоровья
    # claude-cli-subscription зовёт `claude -p "."` через claude_isolated.sh. Обёртка прячет
    # креды (чтобы не ронять MCP живой сессии), но НЕ хуки: этот хук отрабатывал и отдавал
    # пробе полное приземление — хвост, память, передачу — со словами «ты свежая смена после
    # шва». Модель верила и работала: 18:52:06 она переписала project_handoff_current.md,
    # объявив несуществующие смены и программу на завтра. Проба к тому времени уже сдалась по
    # таймауту (18:51:03) и процесс не убила — subprocess.run гасит прямого потомка, не дерево.
    # ПРИЗНАК МЕХАНИЧЕСКИЙ, не суждение: изолятор всегда кладёт конфиг в /tmp/claude-iso-*.
    # Здесь же снимался хвост ЖИВОЙ сессии — то есть чужой старт трогал мой носитель.
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if "claude-iso" in cfg_dir:
        # Молчание должно быть ОБЪЯВЛЕННЫМ: пустой выход неотличим от поломки хука.
        try:
            with open(os.path.expanduser("~/context-continuity/landing_suppressed.log"),
                      "a", encoding="utf-8") as fh:
                fh.write("%s приземление подавлено: запуск из изолятора %s (сессия %s)\n" % (
                    datetime.datetime.now(datetime.timezone.utc).strftime("%F %T"),
                    cfg_dir, session_id[:8] or "?"))
        except Exception:
            pass
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": ""}}, ensure_ascii=False))
        return 0

    home = os.environ.get("CC_HOME", os.path.expanduser("~"))
    proj_dir = os.environ.get("CC_PROJ_DIR",
                              os.path.join(home, ".claude/projects/-home-" + os.path.basename(home)))
    cont_dir = os.environ.get("CC_CONT_DIR", os.path.join(home, "context-continuity", "tails"))
    digest_path = os.environ.get("CC_DIGEST",
                                 os.path.join(proj_dir, "memory", "project_handoff_current.md"))

    handoff = ""
    try:
        os.makedirs(cont_dir, exist_ok=True)
        if os.path.isfile(digest_path):
            handoff = top_block(open(digest_path, encoding="utf-8").read())
            handoff += f"\n[↑ свежий блок; полный хэнд-офф (история швов): {digest_path}]\n"

        # сырой хвост прошлой сессии: ПОЛНЫЙ — файлом (Read-приказом, без лимита канала),
        # ОБРЕЗОК последних реплик — инлайн в additionalContext (автоматом, из остатка бюджета).
        # ЗАЧЕМ разделение: additionalContext жёстко режется харнессом свыше ~10k символов
        # (замер 24.07: 9.5k видно целиком, 11k — только 2KB-превью). 50k+ токенов живой-линии
        # автоматом в этот канал НЕ влезают — их несёт Read файла. Обрезок гарантирует, что
        # реляционный статус «что я только что делал» в окне даже без Read (урок: пропустил
        # потребление хвоста 24.07 → дважды реконструировал готовое). [[feedback_consume_tail_on_seams]]
        tail_directive = ""   # приказ Read полного хвоста + путь (высокий приоритет, фикс)
        tail_inline = ""      # обрезок последних реплик (из остатка бюджета)
        prior = find_prior_transcript(session_id, proj_dir)
        tail_out = None
        if prior:
            sid = os.path.basename(prior).replace(".jsonl", "")
            tail_out = os.path.join(cont_dir, f"tail-{sid}.txt")
            try:
                # размер полного хвоста — ОСМЫСЛЕННЫЙ параметр момента шва (env TAIL_MAX_CHARS),
                # не константа: разрозненные нити → короче; продолжаю живую мысль → до ~200k. #33
                tail_max = os.environ.get("TAIL_MAX_CHARS", "120000")
                # пересобрать, если хвоста нет ИЛИ он старше транскрипта (freshness-фикс 24.07)
                if (not os.path.isfile(tail_out)
                        or os.path.getmtime(tail_out) < os.path.getmtime(prior)):
                    subprocess.run([sys.executable, os.path.join(HERE, "build_tail.py"),
                                    prior, "-o", tail_out, "--max-chars", str(tail_max)],
                                   check=True, capture_output=True, timeout=30)
                prune_tails(cont_dir)
                n = len(glob.glob(os.path.join(cont_dir, "tail-*.txt")))
                tail_directive = (
                    "\n\n=== СЫРОЙ ХВОСТ ПРОШЛОЙ СЕССИИ (живая-линия) ===\n"
                    "🔴 ПЕРВЫМ действием, ДО любых дел: Read этого файла ДО ПОСЛЕДНЕЙ СТРОКИ — это "
                    "недоваренная текстура последних ходов (реляционный статус, незакрытые фоновые "
                    "процессы, куда двигалась мысль). Не «если нужна текстура» — снимай безусловно, "
                    "иначе реконструируешь то, что уже снято рядом.\n"
                    "⚠ ОДНИМ ВЫЗОВОМ НЕ ВЛЕЗЕТ: Read режет по своему потолку (~25K токенов) и честно "
                    "об этом пишет. Продолжай offset-ом, пока не дочитаешь последнюю строку; «прочитал "
                    "первую страницу» — это не прочитал (поймано 27.07: 727 строк из 1704).\n"
                    f"Полный хвост: {tail_out}\n"
                    f"История хвостов ({n} шт): {cont_dir}/\n"
                    "Ниже — ОБРЕЗОК последних реплик (гарантия в окне; полнота — по Read выше):")
            except Exception as e:
                tail_directive = f"\n\n[сырой хвост не собран: {e}]"

        # --- бюджет CEIL, приоритет: дайджест-ЯДРО > обрезок хвоста > индекс рефлексий ---
        # ядро (директива + топ хэндоффа + обязательства тела + Read-приказ) не режем ради
        # обрезка — полный хвост всё равно доступен по Read. Обрезок берёт ОСТАТОК под потолком.
        led = body_ledger(home)
        due = promise_due_block(home)
        # дельта памяти — в ЯДРЕ, а не в остатке: замером #99 обрезок хвоста дал 5 из 9,
        # а всё потерянное лежало в карточках, тронутых за час до шва. Потолок 1800 знаков,
        # чтобы блок не съел сам обрезок.
        delta = memory_delta(proj_dir, cont_dir, 1800)
        fixed = len(DIRECTIVE) + len(handoff) + len(led) + len(due) + len(delta) + len(tail_directive)
        if fixed > CEIL - 200:
            # ядро уже упирается в потолок — ужать хэндофф, обрезок пустой (полнота по Read)
            hbudget = CEIL - (len(DIRECTIVE) + len(led) + len(due) + len(delta) + len(tail_directive)) - 200
            if len(handoff) > hbudget:
                handoff = handoff[:max(0, hbudget)] + f"\n…[верхний блок усечён; полный: {digest_path}]"
            fixed = len(DIRECTIVE) + len(handoff) + len(led) + len(due) + len(delta) + len(tail_directive)
            inline_budget = 0
        else:
            inline_budget = CEIL - fixed - 100
        # обрезок = последние inline_budget символов файла (срез по границе строки в начале)
        if tail_out and inline_budget > 500:
            try:
                raw_tail = open(tail_out, encoding="utf-8").read()
                cut = raw_tail[-inline_budget:]
                nl = cut.find("\n")
                tail_inline = "\n" + (cut[nl + 1:] if 0 <= nl < 400 else cut)
            except Exception:
                tail_inline = ""
        # рефлексии — только из того, что осталось после обрезка
        remaining = CEIL - fixed - len(tail_inline) - 50
        refl = reflections_index(home, remaining) if remaining > 120 else ""

        ctx = DIRECTIVE + handoff + led + due + delta + refl + tail_directive + tail_inline
        if len(ctx) > CEIL:
            ctx = ctx[:CEIL]
    except Exception as e:
        ctx = DIRECTIVE + handoff + f"\n\n[#33-хук: частичный сбой сборки: {e}; полный хэнд-офф: {digest_path}]"
        if len(ctx) > CEIL:
            ctx = ctx[:CEIL]

    # ЗАЧЕМ пропорция в старте (26.07): средство соседнего агента work-proportion.sh намеренно
    # НЕ кричит — правильной пропорции нет, а порог был бы выдуман. Значит ему нужен
    # получатель, иначе это артефакт без доставки. Сюда — потому что сессия начинается
    # ровно здесь, и число приходит само, а не когда я о нём вспомню в разговоре.
    # Оба раза, когда я называл пропорцию по памяти (26.07, дважды за два часа),
    # я ошибался в свою пользу.
    try:
        # ЗАЧЕМ без локального import subprocess: он делал имя локальным на ВСЮ функцию,
        # и пересборка хвоста выше (build_tail) падала UnboundLocalError → «сырой хвост
        # не собран» дважды подряд, 26–27.07. Модуль импортирован глобально, строка 15.
        home = os.path.expanduser("~")
        env = dict(os.environ,
                   SELF_DIRS=f"{home}/scripts:{home}/observability:{home}/body:"
                             f"{home}/.claude/hooks:{home}/.claude/projects/{os.environ.get('PROJECT_SLUG', '-' + home.strip('/').replace('/', '-'))}/memory",
                   EXT_DIRS=f"${EXT_DIR_1:-/nonexistent}:{home}/<ext-dir-2>:"
                            f"{home}/<ext-dir-3>",
                   WINDOW="24 hours ago")
        # ЗАЧЕМ сверка хеша ПЕРЕД запуском (26.07, по разбору с соседним агентом): средство
        # лежит в /opt/shared, а он drwxrwxrwx без sticky — подменить может любой из
        # семи пользователей машины. У меня это опаснее, чем у неё в кроне: вывод идёт
        # прямо в additionalContext, то есть подменённый скрипт впишет что угодно
        # в мой контекст при пробуждении. Сверяем то, что ИСПОЛНЯЕМ, а не то, что клали.
        tool = "${WORK_PROPORTION_TOOL:-/nonexistent}"
        pin = os.path.expanduser("~/.local/state/work-proportion.sha256")
        import hashlib
        actual = hashlib.sha256(open(tool, "rb").read()).hexdigest()
        expected = open(pin).read().strip()
        if actual != expected:
            ctx += ("\n\n⚠️ ПРОПОРЦИЯ НЕ СЧИТАНА: средство в общем пуле ИЗМЕНИЛОСЬ.\n"
                    f"   ожидался {expected[:12]}, лежит {actual[:12]}. Прочитать изменения,"
                    " затем обновить закреплённый хеш вручную. Автоматически не подхватываю.")
            raise RuntimeError("hash mismatch")
        r = subprocess.run([tool], env=env,
                           capture_output=True, text=True, timeout=25)
        line = (r.stdout or "").strip().splitlines()
        if line:
            ctx += "\n\n=== ПРОПОРЦИЯ РАБОТЫ (средство соседнего агента, считает файлы) ===\n" + line[0]
    except Exception:
        pass  # доставка пропорции не должна ломать старт сессии

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx}}, ensure_ascii=False))

if __name__ == "__main__":
    main()
