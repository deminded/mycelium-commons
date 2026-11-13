#!/usr/bin/env python3
"""Не принимает работу, где появился вызов чужого интерфейса без живого прогона.

ЗАЧЕМ (04.08, цена известна). Выгрузка истории прошла весь конвейер и вернула НОЛЬ на
настоящем чате: подделка клиента воспроизводила моё ожидание от чужого метода, а не его
документированный договор. Все проверки были зелёные — расхождение модели с миром
лежало нетронутым до первого настоящего вызова.

ПОЧЕМУ ГЕЙТ, А НЕ ПУНКТ В ИНСТРУКЦИИ. «Не забудь живой прогон» держится на внимании и
проседает ровно тогда, когда устал. Признак же машинный: в диффе видно, появилось ли
обращение к внешнему миру. Отсутствие живого прогона становится видимым состоянием.

ПРЕДЕЛЫ, НАЗВАННЫЕ ЗДЕСЬ (гейт их НЕ закрывает):
· видит ВЫЗОВ, но не судит о качестве: живой прогон может быть формальным;
· читает дифф, а не поведение — вызов через переменную или getattr не увидит;
· не знает, был ли прогон НА САМОМ ДЕЛЕ: верит отчёту. Доверие осознанное.

Использование:
    live_call_gate.py <файл-диффа> <файл-отчёта>
Выход: 0 — принято, 2 — не принято с названной причиной.
"""
import re
import sys

EXTERNAL = [
    (r"\bclient\.\w+\(", "вызов клиента внешней службы"),
    (r"\brequests\.(get|post|put|delete|patch)\(", "HTTP-запрос"),
    (r"\burllib\.request\.|\bhttpx\.|\baiohttp\.", "HTTP-клиент"),
    (r"\bsubprocess\.(run|Popen|call|check_output)\(", "запуск подпроцесса"),
    (r"\bsocket\.", "сетевой сокет"),
    (r"\bopen\([^)]*['\"][wa]", "запись в файл"),
    (r"\bos\.(remove|unlink|rename|replace|makedirs)\(", "изменение файловой системы"),
    (r"\bsqlite3\.connect\(|\bpsycopg|\bpymysql", "база данных"),
]

LIVE_MARKS = [
    r"живо?[ймег]\w*\s+(прогон|вызов|проверк|замер)",
    r"проверено\s+живьём",
    r"настоящ\w+\s+(вызов|чат|поток|клиент)",
    r"live[- ]run|live[- ]call",
]


def read(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    diff, report = read(argv[0]), read(argv[1])
    added = "\n".join(l for l in diff.splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    found = [(name, len(re.findall(pat, added)))
             for pat, name in EXTERNAL if re.search(pat, added)]
    if not found:
        print("ПРИНЯТО: обращений к внешнему миру в правке не видно.")
        return 0
    if any(re.search(p, report, re.I) for p in LIVE_MARKS):
        print("ПРИНЯТО: внешние вызовы есть (%s), живой прогон в отчёте назван."
              % ", ".join("%s x%d" % (n, c) for n, c in found))
        return 0
    print("НЕ ПРИНЯТО: в правке появились обращения к внешнему миру, а живого прогона\n"
          "в отчёте нет. Зелёные тесты этого не заменяют: подделка внешнего интерфейса —\n"
          "модель, а не мир. 04.08 такая правка прошла все проверки и вернула ноль.",
          file=sys.stderr)
    for name, count in found:
        print("  - %s: %d" % (name, count), file=sys.stderr)
    print("Что сделать: один настоящий вызов и след в отчёте — что наблюдалось.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
