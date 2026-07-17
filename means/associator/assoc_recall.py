#!/usr/bin/env python3
"""
UserPromptSubmit hook — associative memory recall.

Silently injects top-K semantically unexpected but relevant memory nodes
into the conversation context. Any error → exit 0 with empty output (never blocks).
"""
import json
import os
import re
import sys
import signal

# Hard deadline for entire hook (cache load + PPR run)
TIMEOUT_SECS = 4

# Fork config: где лежит пакет ассоциатора (ppr_memory_v2.py + журнал выдач).
# Дефолта НЕТ: подставленный путь автора заставил бы хук чужого харнесса грузить
# ядро из несуществующего у него каталога — и молчать об этом, ибо хук по контракту
# глотает все ошибки. Тишина здесь неотличима от «памяти нечего сказать».
#
# Но требовать env остановкой (как в ядре) хук НЕ вправе: он сидит в петле диалога,
# и его падение ломает не средство, а разговор форкающего. Отсюда типизация:
# инструмент-в-руках обязан требовать, инструмент-в-петле обязан уступать —
# громко в stderr, тихо в stdout.
ASSOCIATOR_HOME = os.environ.get("MYCELIUM_ASSOCIATOR_HOME")

# Рамка task-notification (Claude Code) лексически жирнее содержания: на живых
# промахах ассоциатор отвечал портретом ОБЁРТКИ (tool-use-id, /tmp/-пути, теги
# давали устойчивый ложный фон), а под ней 4/5 оказались попаданиями.
# Срезаем рамку, оставляем summary — предмет; внутри summary не режем ничего.
# Контракт тега — харнессный: у всякого форка на Claude Code рамка та же.
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


def strip_frame(prompt: str):
    """Return (clean_text, was_stripped). Пустой summary → пустая строка:
    молчание честнее уверенного фона от упаковки."""
    if "<task-notification>" not in prompt:
        return prompt, False
    summaries = [s.strip() for s in _SUMMARY_RE.findall(prompt) if s.strip()]
    if not summaries:
        return "", True
    return "\n".join(summaries), True


def _handle_timeout(signum, frame):
    raise TimeoutError("assoc_recall timeout")


def main() -> None:
    try:
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.alarm(TIMEOUT_SECS)

        data = json.load(sys.stdin)
        prompt = data.get("prompt", "").strip()
        prompt, stripped = strip_frame(prompt)

        # Skip very short prompts — not enough signal for associator
        # (порог ПОСЛЕ среза: уведомление без предметного summary молчит,
        # а не печатает фон с уверенным видом)
        if len(prompt) < 8:
            print("{}", flush=True)
            return

        if not ASSOCIATOR_HOME:
            print(
                "[ассоциатор] MYCELIUM_ASSOCIATOR_HOME не задан — каталог пакета "
                "(там же ppr_memory_v2.py). Хук пропускает ход, диалог не тронут.",
                file=sys.stderr,
            )
            print("{}", flush=True)
            return

        sys.path.insert(0, ASSOCIATOR_HOME)
        from ppr_memory_v2 import load_cache, associate

        cache = load_cache()
        results = associate(prompt, cache=cache)

        signal.alarm(0)  # cancel deadline

        # Наблюдаемость: журнал выдач для оценки пользы слоя (вопрос Евгения 03.07).
        # Пишем то, что ВИДЕЛ associate (очищенный вход), не сырую обёртку:
        # у уведомлений первые ~100 символов = одна рамка, и журнал прибора
        # нельзя было поверить по нему же. Записи со срезанной рамкой несут stripped.
        try:
            import datetime
            rec = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "prompt": prompt[:200],
                "hits": [r["label"] for r in (results or [])],
            }
            if stripped:
                rec["stripped"] = True
            with open(os.path.join(ASSOCIATOR_HOME, "assoc_log.jsonl"), "a") as lf:
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

        if not results:
            print("{}", flush=True)
            return

        lines = [
            "🧠 Возможно релевантное из памяти (ассоциатор, фон — не инструкция):"
        ]
        for r in results:
            lines.append(f"— {r['label']}: {r['snippet']}")

        ctx = "<associative-recall>\n" + "\n".join(lines) + "\n</associative-recall>"

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": ctx,
            }
        }), flush=True)

    except Exception as e:
        # Ход диалога хук не ломает (stdout пуст) — но и молчать о поломке не вправе:
        # тишина ассоциатора неотличима от «памяти нечего сказать». Собственная
        # типизация файла (шапка) требует «громко в stderr, тихо в stdout» — до сей
        # правки except исполнял только вторую половину: норма жила комментарием.
        signal.alarm(0)
        print(f"[ассоциатор] пропускаю ход: {type(e).__name__}: {e}", file=sys.stderr)
        print("{}", flush=True)


if __name__ == "__main__":
    main()
