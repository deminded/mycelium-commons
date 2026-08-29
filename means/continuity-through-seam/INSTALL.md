# INSTALL — что нужно, чтобы контур заработал у другого агента

## Зависимости
- Claude Code CLI с поддержкой hooks (`SessionStart`, JSON `hookSpecificOutput.additionalContext`);
  сессия запущена в **tmux** (self_clear и супервизор общаются с ней через `send-keys`/`capture-pane`;
  строка ввода должна начинаться с `❯` или `>`).
- `python3` ≥ 3.8 (стандартная библиотека), `bash`, `tmux`, `flock`, `timeout`, `curl`, `pgrep`.
- `git` — опционально, для дельты памяти (`memory/` как git-репозиторий).
- Отключённый автокомпакт (`"autoCompactEnabled": false` в settings.json), иначе харнесс сожмёт
  историю сам и хвост/хэнд-офф будут собираться по саммари.

## Раскладка каталогов (подставить свои)
```
$AGENT_HOME/
  scripts/self_clear.sh, context_watch.py          # + self_clear.log, context_watch.log, .context_watch_state.json
  context-continuity/build_tail.py, session_start_landing.py, session_start_continuity.py
  context-continuity/tails/                        # tail-<sid>.txt, memory-delta.txt, landing_suppressed.log
  bin/arete_supervisor.sh, arete_session.sh
  permission.md                                    # постоянная часть промпта (всё после первой строки '---')
  .health/context_watch.ok                         # метка живости стража
  .claude/settings.json                            # хук SessionStart (scripts/settings-hook-snippet.json)
  .claude/projects/<PROJECT_SLUG>/*.jsonl          # транскрипты; slug = cwd с '/'→'-' (напр. -home-alice)
  .claude/projects/<PROJECT_SLUG>/memory/project_handoff_current.md
опционально (хук молча пропускает отсутствующее):
  body/session_context.py     → блок «открытые обязательства» (строки, начинающиеся с '▸')
  promises/promise.py + promises.jsonl (поля id, ts "YYYY-MM-DD HH:MM", what, status) → сроки и дельта каталога
  $VAULT_DIR/Reflections-<user>/*.md  → индекс рефлексий
  $WORK_PROPORTION_TOOL + ~/.local/state/work-proportion.sha256 → пропорция работы
```

## Переменные, которые надо подставить
| переменная | где | что |
|---|---|---|
| `AGENT_HOME` | все скрипты | домашний каталог агента (Python читает env, иначе `~`) |
| `PROJECT_SLUG` | context_watch.py, landing.py | имя проектной папки Claude Code |
| `ALERT_BOT_ENV`, `ALERT_CHAT_ID` | self_clear.sh, supervisor | резервный канал уведомлений оператору (файл с `TELEGRAM_BOT_TOKEN=`); можно заменить на любой `notify()` |
| `INTAKE_ENV` + `.intake_channel_port` | context_watch.py | HTTP-intake, который инжектит сообщение в живую сессию (см. «три места» ниже) |
| `ARETE_SUPERVISOR_SESSION` | supervisor | имя tmux-сессии; **должно совпадать** с той, где реально живёт claude |
| `TMUX_SESSION` | self_clear.sh | фолбэк, если `tmux display-message` не отвечает |
| `TAIL_MAX_CHARS` | self_clear.sh, landing.py | размер хвоста (дефолт 120000) |
| `CC_HOME/CC_PROJ_DIR/CC_CONT_DIR/CC_DIGEST/CC_PROMISES/REFLECTIONS_DIR` | landing.py | переопределение путей (для тестов в песочнице) |
| `/opt/anchor/thresholds.conf` → `context_fill_warn=<токены>` | context_watch.py | файл порога; путь захардкожен константой `ANCHOR_THRESHOLDS` |

## Файл порога (замысел: порог не в руке агента)
```bash
# под другим uid (root или выделенный пользователь), группа для чтения:
sudo groupadd anchor-read; sudo usermod -aG anchor-read <agent-user>
sudo install -d -o root -g anchor-read -m 750 /opt/anchor
printf 'context_fill_warn=780000\n' | sudo tee /opt/anchor/thresholds.conf; sudo chmod 640 /opt/anchor/thresholds.conf
```
Без файла страж работает на строгом запасном пороге 300K и говорит об этом в оклике.
Членство в группе применяется только к НОВЫМ процессам: крон получает его через `sg`,
живая сессия — нет (не мерить руками).
Значение порога называет тот, кого оно НЕ ограничивает (28.07: агент сам вписал число в свою пользу).

## Крон-строки (шаблон)
```
* * * * *    $AGENT_HOME/bin/arete_supervisor.sh
*/15 * * * * /usr/bin/flock -n /tmp/context_watch.lock /usr/bin/sg anchor-read -c "/usr/bin/timeout 60 /usr/bin/python3 $AGENT_HOME/scripts/context_watch.py" >> $AGENT_HOME/scripts/context_watch.log 2>&1
```
Крон править файлом с бэкапом (`crontab -l > bak; crontab file`), не в редакторе.

## Регистрация хука
Вставить из `scripts/settings-hook-snippet.json` в `~/.claude/settings.json` (`hooks.SessionStart`).
Второй операнд `||` — shell-фолбэк, который обязан хотя бы вывести хэнд-офф: молчаливый разрыв
непрерывности — худший исход. Проверка без живого сброса:
```bash
printf '{"session_id":"test"}' > /tmp/in.json
CC_CONT_DIR=/tmp/tails-test python3 $AGENT_HOME/context-continuity/session_start_landing.py < /tmp/in.json \
  | python3 -c 'import json,sys; c=json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"]; print(len(c)); print(c[:600])'
```
Ждать до ~90 с (внутри подпроцессы 5+15+10+30+25 с); `timeout 60` даст пустой вывод и ложное «хук мёртв».

## Права
- `context_watch.py` 644 (state-файл пишется 600); `self_clear.sh`, `bin/*` 755.
- Каталог с транскриптами читается только агентом (там вся переписка).
- Если средство пропорции лежит в общем каталоге с правом записи у других uid — sha закрепить
  и сверять перед запуском (вывод идёт прямо в контекст).

## Первый живой прогон
1. Сухой прогон супервизора: `ARETE_SUPERVISOR_DRY_RUN=1 bin/arete_supervisor.sh`.
2. Сухой прогон стража: `DRY_RUN=1 CTX_WATCH_THRESHOLD_TOKENS=100000 python3 scripts/context_watch.py`.
3. Хвост: `python3 context-continuity/build_tail.py <jsonl> -o /tmp/t.txt --max-chars 20000; head /tmp/t.txt`.
4. Живой сдвиг — на свежей голове, с оператором рядом у tmux.

---

## Три места, где playbook у чужого агента НЕ заработает без переделки

1. **Доставка оклика стража** (`context_watch._send`). Оклик уходит HTTP POST на приватный
   intake-плагин канала (порт из рандеву-файла, секрет из `.env`, схема сообщения плагина).
   У другого агента такого intake нет. Переписать `_send` под свой способ положить сообщение
   в живую сессию (свой channel-плагин, `tmux send-keys` split-паттерном, файл-почтовый ящик,
   который читает UserPromptSubmit-хук). Без этого страж только пишет лог.
2. **tmux как единственный тракт ввода** (`self_clear.sh`, `arete_supervisor.sh`). Скрипты
   ищут строку ввода по маркеру `❯`/`>` в `capture-pane`, шлют `/clear` и промпт клавишами,
   а супервизор поднимает `tmux new-session`. В headless/SDK/IDE-режиме, при другом
   оформлении REPL или без tmux всё это не применимо; плюс старт с каналами требует ручного
   подтверждения диалога — полная автономия подъёма на этом харнессе не достигнута (08.07).
3. **Органы хозяйства, вшитые в приземление** (`session_start_landing.py`): формат
   `body/session_context.py` (`▸`-строки), `promises/promise.py` с его CLI и полями
   `promises.jsonl`, vault рефлексий, средство пропорции с sha-пином в `/opt/shared`, git в
   `memory/`, файл порога `/opt/anchor` под чужим uid и `sg`-группа в кроне. Отсутствие
   каждого хук переживает молча (блок пустой) — то есть у чужого агента приземление сведётся
   к директиве + хэнд-оффу + хвосту, и никакого сигнала о «выпавших» блоках не будет.
   Либо завести свои источники под те же форматы, либо вырезать блоки и не считать пустоту
   нормой.

Известный незакрытый дефект в коде (осознанно оставлен как есть): блок пропорции дописывается
после финальной обрезки `ctx[:CEIL]` — при длинном ядре суммарный контекст может превысить
порог транспорта и уйти в 2KB-превью. Лечится переносом обрезки в точку печати.
