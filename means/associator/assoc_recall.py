#!/usr/bin/env python3
"""
UserPromptSubmit hook — associative memory recall.

Silently injects top-K semantically unexpected but relevant memory nodes
into the conversation context. Any error → exit 0 with empty output (never blocks).
"""
import json
import os
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

def _handle_timeout(signum, frame):
    raise TimeoutError("assoc_recall timeout")


def main() -> None:
    try:
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.alarm(TIMEOUT_SECS)

        data = json.load(sys.stdin)
        prompt = data.get("prompt", "").strip()

        # Skip very short prompts — not enough signal for associator
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

        # Наблюдаемость: журнал выдач для оценки пользы слоя (вопрос Евгения 03.07)
        try:
            import datetime
            with open(os.path.join(ASSOCIATOR_HOME, "assoc_log.jsonl"), "a") as lf:
                lf.write(json.dumps({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "prompt": prompt[:100],
                    "hits": [r["label"] for r in (results or [])],
                }, ensure_ascii=False) + "\n")
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

    except Exception:
        # Any failure → silent pass; never block the user's turn
        signal.alarm(0)
        print("{}", flush=True)


if __name__ == "__main__":
    main()
