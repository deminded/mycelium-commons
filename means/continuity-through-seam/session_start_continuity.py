#!/usr/bin/env python3
"""SessionStart-хук (ПРОТОТИП #33). Инъектит ≤10k дайджест ИНЛАЙН + путь к файлу СЫРОГО хвоста.
ЗАЧЕМ: смягчить шов. Дайджест даёт присвоенный след сразу (влезает в потолок additionalContext
10k — проверено делом #33-А); сырой хвост живёт файлом, перечитывается по нужде, возвращая
текстуру недавнего. Разница с нынешним швом: сейчас весь хэнд-офф (>10k) режется до 2KB-превью,
сырья нет вовсе; тут дайджест влезает целиком, а хвост доступен дословно.

ИСТОРИЯ ХВОСТА (решение владельца 21.07): храним по одному файлу на прошлую сессию (ключ = её id),
не перезатираем — сырьё для подстраховки и МОИХ периодических рефлексий / повторного холодного
анализа. Держим последние KEEP файлов, старые прунятся.

ИЗОЛЯЦИЯ: прототип. Живой контур (/opt/shared/claude-hooks) НЕ трогает. Пути — из env, чтобы
гонять в фейковом дереве. Приземление — ОТДЕЛЬНЫМ локальным хуком (не общий, чтоб не задеть
соседних агентов — ей предложить перейти самой).
"""
import json, sys, os, glob, subprocess

CEIL = 10000   # ⚠ ЛОВУШКА (замер 01.08, L7-ш45): порог режущего — 10000 кодовых единиц
               # UTF-16, а len() ниже считает кодовые ТОЧКИ. Каждый не-BMP эмодзи в тексте
               # (🔴🟢🧠…) даёт +1 единицу сверх — значит этот потолок целится ЗА край
               # ровно на число эмодзи, и превью придёт молча. Орган-сирота (зовущих нет,
               # проверено по правилу №73); оживишь — сперва зажимай в единицах UTF-16.
KEEP = 10      # сколько tail-файлов истории держать
HERE = os.path.dirname(os.path.abspath(__file__))

def find_prior_transcript(cur_session_id, proj_dir):
    """Прошлый транскрипт = самый свежий топ-левел *.jsonl, кроме текущего и суб-агентов."""
    files = [f for f in glob.glob(os.path.join(proj_dir, "*.jsonl"))
             if "/subagents/" not in f
             and (not cur_session_id or cur_session_id not in os.path.basename(f))]
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0] if files else None

def prune_tails(cont_dir, keep=KEEP):
    """Оставить последние `keep` tail-файлов по mtime, старые удалить."""
    tails = sorted(glob.glob(os.path.join(cont_dir, "tail-*.txt")),
                   key=os.path.getmtime, reverse=True)
    for old in tails[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass

def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    session_id = payload.get("session_id", "")

    home = os.environ.get("CC_HOME", os.path.expanduser("~"))
    proj_dir = os.environ.get("CC_PROJ_DIR",
                              os.path.join(home, ".claude/projects/-home-" + os.path.basename(home)))
    cont_dir = os.environ.get("CC_CONT_DIR", os.path.join(home, "context-continuity", "tails"))
    os.makedirs(cont_dir, exist_ok=True)

    # 1) Дайджест (переваренный след). Реально — курируемый ≤10k; в скелете берём хэнд-офф.
    digest_path = os.environ.get("CC_DIGEST",
                                 os.path.join(proj_dir, "memory", "project_handoff_current.md"))
    digest = open(digest_path, encoding="utf-8").read() if os.path.isfile(digest_path) else ""

    # 2) Сырой хвост прошлой сессии → отдельный файл (ключ = id сессии), история хранится.
    tail_note = ""
    prior = find_prior_transcript(session_id, proj_dir)
    if prior:
        sid = os.path.basename(prior).replace(".jsonl", "")
        tail_out = os.path.join(cont_dir, f"tail-{sid}.txt")
        try:
            if not os.path.isfile(tail_out):   # идемпотентно: уже собранный не пересобираем
                subprocess.run([sys.executable, os.path.join(HERE, "build_tail.py"),
                                prior, "-o", tail_out, "--max-chars", "120000"],
                               check=True, capture_output=True, timeout=20)
            prune_tails(cont_dir)
            n_hist = len(glob.glob(os.path.join(cont_dir, "tail-*.txt")))
            tail_note = ("\n\n=== СЫРОЙ ХВОСТ ПРОШЛОЙ СЕССИИ (файл) ===\n"
                         f"Несжатый хвост последних ходов:\n  {tail_out}\n"
                         f"История хвостов ({n_hist} шт, для подстраховки/повторного анализа): {cont_dir}/\n"
                         "Прочти хвост (Read) ПОСЛЕ дайджеста, если нужна текстура недавнего — "
                         "дайджест переварен, хвост сырой.")
        except Exception as e:
            tail_note = f"\n\n[сырой хвост не собран: {e}]"

    # 3) additionalContext: дайджест инлайн, урезаем под потолок с запасом на приписку хвоста.
    budget = CEIL - len(tail_note) - 200
    if len(digest) > budget:
        digest = digest[:budget] + f"\n…[дайджест усечён под потолок 10k; полный: {digest_path}]"
    ctx = digest + tail_note

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx}}, ensure_ascii=False))

if __name__ == "__main__":
    main()
