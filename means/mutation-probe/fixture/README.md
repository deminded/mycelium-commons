# fixture для mutate.py — три состояния, три команды

Предмет: `guarded.py` (проверка подписи + непокрытый тестом лимит длины).
Защита: `test_guard.py` (три случая, у каждого есть способ проиграть).

Запускать из этого каталога. Ожидаемые коды: 2 = UNVERIFIED, 0 = FAIL-SAFE, 1 = HOLE.

## 1. UNVERIFIED — якорь неуникален (в файле два `return False`)

    python3 ../mutate.py --file guarded.py \
      --anchor "return False" --replacement "return True" \
      --test "python3 -m pytest -q" --receipt r1.json

## 2. FAIL-SAFE — защита сломана, тест обязан покраснеть

    python3 ../mutate.py --file guarded.py \
      --anchor "if signature != sign(text):" --replacement "if False:" \
      --test "python3 -m pytest -q" --receipt r2.json

## 3. HOLE — мутация применена, тест её не видит

    python3 ../mutate.py --file guarded.py \
      --anchor "MAX_LEN = 1000" --replacement "MAX_LEN = 100000" \
      --test "python3 -m pytest -q" --receipt r3.json

Случай 3 — настоящая дыра предмета: ОСЛАБЛЕНИЕ лимита длины ничем не проверяется,
и это видно не из рассуждения, а из кода возврата.

Здесь же — след моей ошибки, оставленный намеренно. Сначала я поставил в случай 3
замену `MAX_LEN = 1` и ждал HOLE, а получил FAIL-SAFE: ужесточение лимита роняет
тест на валидной подписи, потому что тексты в тестах длиннее одного знака. То есть
я собрал fixture по своему представлению о покрытии, а не по факту, и обнаружил
это только прогоном. Ровно тот класс, ради которого инструмент и сделан.
