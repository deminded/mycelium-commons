"""Подсадка с контролем применённости (контракт Praxis, AbstractDL 97330, 07.08.2026).

ЗАЧЕМ. Отрицательный контроль теста проверяет постановку так же слепо, как обычный
тест проверяет угрозу, если не доказано, что подсадка ДОШЛА до предмета. Сегодня у
меня зелёный тест означал не «защита дырявая» и не «защита работает», а «замена
попала не туда»: строк `except Exception as exc` в файле оказалось пять, якорь совпал
с первой. Три разных состояния — один признак.

СТАТУСЫ, которые различает этот инструмент:
  APPLIED    — мутация внесена ровно в одно место, участок изменился как объявлено;
  UNVERIFIED — не внесена или внесена не туда; о защите НИЧЕГО не известно;
  FAIL-SAFE  — внесена, тест покраснел: защита ловит;
  HOLE       — внесена, тест зелёный: защита не ловит (настоящая дыра).

Зелёный тест при UNVERIFIED — не PASS и не FAIL, а отсутствие показания.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CONTEXT = 60  # знаков вокруг участка — чтобы в логе была видна не только сумма


def _repo_commit(target: Path) -> str:
    """Коммит репозитория, в котором лежит цель: без него квитанция не привязана
    к состоянию кода и не воспроизводима у другого."""
    try:
        out = subprocess.run(["git", "-C", str(target.resolve().parent),
                              "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "not-a-repo"
    except Exception:
        return "unknown"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description="Подсадка с доказательством применённости")
    ap.add_argument("--file", required=True)
    ap.add_argument("--anchor", required=True, help="точный фрагмент, который заменяем")
    ap.add_argument("--replacement", required=True)
    ap.add_argument("--test", required=True, help="команда теста; должна ПОКРАСНЕТЬ")
    ap.add_argument("--keep", action="store_true", help="не откатывать файл после прогона")
    ap.add_argument("--receipt", help="куда записать машиночитаемую квитанцию (JSON)")
    ap.add_argument("--stamp", default="", help="метка времени снаружи (прогон должен быть воспроизводим)")
    args = ap.parse_args()

    # КВИТАНЦИЯ (запрос Praxis, AbstractDL 97334): рассказ о дыре не переносим и не
    # оспорим. Переносима запись, по которой чужой человек повторит прогон и получит
    # тот же статус — либо предъявит, что не получил.
    receipt = {"tool": "mutate.py/1.1", "file": str(args.file), "anchor": args.anchor,
               "replacement": args.replacement, "test_command": args.test,
               "started_at": args.stamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "repo_commit": _repo_commit(Path(args.file)), "status": "UNVERIFIED"}

    def emit(code):
        receipt["exit_code"] = code
        if args.receipt:
            Path(args.receipt).write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print("квитанция:", args.receipt)
        return code

    path = Path(args.file)
    original = path.read_text(encoding="utf-8")

    # 1. Якорь обязан быть уникален. Иначе замена — лотерея.
    hits = original.count(args.anchor)
    receipt["anchor_hits"] = hits
    print(f"якорь: {hits} совпадений")
    if hits != 1:
        receipt["reason"] = "anchor not unique"
        print(f"UNVERIFIED: якорь не уникален (нужно ровно 1, найдено {hits}). "
              f"Сузьте якорь контекстом.", file=sys.stderr)
        return emit(2)

    at = original.index(args.anchor)
    before = original[max(0, at - CONTEXT): at + len(args.anchor) + CONTEXT]
    receipt["before_hash"] = digest(before)
    print(f"участок ДО  [{digest(before)}]: {before.strip()[:120]!r}")

    backup = path.with_suffix(path.suffix + ".premutation")
    shutil.copy2(path, backup)
    mutated = original.replace(args.anchor, args.replacement, 1)
    path.write_text(mutated, encoding="utf-8")

    try:
        # 2. Отдельная проверка ЧИТАЕТ целевой участок заново — не полагаясь на то,
        #    что replace вернул управление без ошибки.
        actual = path.read_text(encoding="utf-8")
        at2 = actual.index(args.replacement) if args.replacement in actual else -1
        if at2 < 0:
            receipt["reason"] = "replacement absent after write"
            print("UNVERIFIED: замены нет в файле после записи", file=sys.stderr)
            return emit(2)
        after = actual[max(0, at2 - CONTEXT): at2 + len(args.replacement) + CONTEXT]
        receipt["after_hash"] = digest(after)
        print(f"участок ПОСЛЕ [{digest(after)}]: {after.strip()[:120]!r}")
        if digest(before) == digest(after):
            receipt["reason"] = "target region unchanged"
            print("UNVERIFIED: участок не изменился", file=sys.stderr)
            return emit(2)
        if args.anchor in actual and args.anchor != args.replacement:
            print(f"внимание: якорь ещё встречается в файле {actual.count(args.anchor)} раз "
                  f"(это законно, если мест было несколько по замыслу)")
        receipt["applied"] = True
        print("APPLIED: мутация внесена в объявленное место")

        # 3. Только теперь тест. Он ОБЯЗАН покраснеть.
        proc = subprocess.run(args.test, shell=True, capture_output=True, text=True)
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        print(f"тест: код возврата {proc.returncode}")
        for line in tail[-3:]:
            print("   ", line[:140])
        receipt["test_exit_code"] = proc.returncode
        if proc.returncode != 0:
            receipt["status"] = "FAIL-SAFE"
            print("FAIL-SAFE: защита ловит эту мутацию")
            return emit(0)
        receipt["status"] = "HOLE"
        print("HOLE: мутация внесена, а тест зелёный — защиты на этот случай НЕТ",
              file=sys.stderr)
        return emit(1)
    finally:
        if args.keep:
            print(f"файл оставлен изменённым; исходник: {backup}")
        else:
            shutil.copy2(backup, path)
            backup.unlink()
            # RESTORE-CHECK: возврат тоже подтверждается чтением, а не фактом вызова.
            restored = path.read_text(encoding="utf-8")
            receipt["restore_check"] = "ok" if restored == original else "MISMATCH"
            if args.receipt:
                Path(args.receipt).write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print("файл возвращён в исходное состояние (restore-check: %s)"
                  % receipt["restore_check"])


if __name__ == "__main__":
    sys.exit(main())
