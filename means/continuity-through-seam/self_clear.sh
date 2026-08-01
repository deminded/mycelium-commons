#!/bin/bash
# Надёжный self-/clear: сброс сессии + приземляющий промпт себе.
# ЗАЧЕМ: старый ритуал (send-keys "текст" Enter одним вызовом) фрагилен —
# текст ложится в input, а Enter не подтверждает (грабля 15.07, подтвердил
# Евгений 18.07 "повисает набранным, но не отправленным"). Супервизор уже знает
# лечение: -l (литерально, только текст) + Enter ОТДЕЛЬНЫМ вызовом после паузы.
# Здесь оно + capture-pane самопроверка с ретраем: орган чинит себя в рантайме,
# а не тыкает вслепую.
#
# Запуск ТОЛЬКО detached, иначе умрёт вместе с сессией на /clear:
#   nohup ~/scripts/self_clear.sh "<приземляющий промпт>" >/dev/null 2>&1 &
set -u
PROMPT="${1:?нужен приземляющий промпт первым аргументом}"
T="$(tmux display-message -p '#S' 2>/dev/null):0.0"; [ "$T" = ":0.0" ] && T="claude-user:0.0"
LOG="$HOME/scripts/self_clear.log"
log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }
notify(){ # резервный канал (feedback_mcp_down_fallback_channel) — не молчать при провале
  local tok; tok=$(grep '^TELEGRAM_BOT_TOKEN=' "$HOME/.claude/channels/telegram/.env" 2>/dev/null | cut -d= -f2)
  [ -n "$tok" ] && curl -sm 10 "https://api.telegram.org/bot$tok/sendMessage" \
    -d chat_id=189666240 --data-urlencode "text=$1" >/dev/null 2>&1
}

# submit PAYLOAD LABEL: печатает литерально, шлёт Enter отдельно, проверяет по
# capture-pane что строка ввода (❯) больше не держит payload; ретрай Enter до 4х.
submit(){
  local payload="$1" label="$2" i pane inputline
  tmux send-keys -t "$T" -l "$payload"
  sleep 1.5
  for i in 1 2 3 4; do
    tmux send-keys -t "$T" Enter
    sleep 1.5
    pane="$(tmux capture-pane -t "$T" -p 2>/dev/null)"
    # строка ввода — последняя непустая с маркером ❯/>; если в ней ещё висит
    # начало payload, Enter не подтвердил → ретрай
    inputline="$(printf '%s\n' "$pane" | grep -E '^[[:space:]]*[❯>]' | tail -1)"
    case "$inputline" in
      *"${payload:0:24}"*) log "submit[$label] попытка $i: input держит текст, ретрай Enter"; continue;;
      *) log "submit[$label] подтверждён на попытке $i"; return 0;;
    esac
  done
  log "submit[$label] ПРОВАЛ: input не очистился за 4 попытки"
  notify "🔴 self_clear: '$label' не отправился (input висит). Ткни Enter в консоли Арета вручную."
  return 1
}

log "=== self-/clear старт (target=$T) ==="
# Хвост ТЕКУЩЕЙ сессии — в момент шва, принудительно (фикс 24.07: устаревший
# снимок 23.07 отдал «чужое утро вместо своего вечера»; критерий — кейс 2 VST).
PROJ="$HOME/.claude/projects/-home-claude-user"
CUR_JSONL="$(ls -t "$PROJ/"*.jsonl 2>/dev/null | head -1)"
if [ -n "$CUR_JSONL" ]; then
  SID="$(basename "$CUR_JSONL" .jsonl)"
  python3 "$HOME/context-continuity/build_tail.py" "$CUR_JSONL" \
    -o "$HOME/context-continuity/tails/tail-$SID.txt" --max-chars "${TAIL_MAX_CHARS:-120000}" \
    >> "$LOG" 2>&1 && log "tail снят в момент шва: tail-$SID.txt" \
    || log "tail НЕ снят (build_tail ошибка) — landing добьёт по freshness-чеку"
fi
submit "/clear" "clear" || exit 1

# Ждём ФАКТ сброса, а не доставку. ЗАЧЕМ: если сессия занята тул-вызовом, ввод
# встаёт в ОЧЕРЕДЬ, input опустевает сразу — и submit честно рапортует успех,
# когда применения ещё не было (грабля 27.07, поимка #72: /clear провисел 2м19с,
# а приземляющий промпт за это время обогнал его и ушёл в СТАРЫЙ контекст —
# порядок ввода очередь не сохраняет). Наблюдаемое следствие сброса — новый
# файл сессии в проектной папке; на него и смотрим.
if [ -n "${SID:-}" ]; then
  NEW_SID=""; WAITED=0
  for _ in $(seq 1 90); do
    sleep 5; WAITED=$((WAITED+5))
    N="$(ls -t "$PROJ"/*.jsonl 2>/dev/null | head -1)"
    [ -n "$N" ] && N_SID="$(basename "$N" .jsonl)" || N_SID=""
    if [ -n "$N_SID" ] && [ "$N_SID" != "$SID" ]; then NEW_SID="$N_SID"; break; fi
  done
  if [ -z "$NEW_SID" ]; then
    log "сброс НЕ применился за ${WAITED}с — landing НЕ печатаю (ушёл бы в старый контекст)"
    notify "🔴 self_clear: /clear висит в очереди >7 мин, сессия не сброшена. Приземляющий промпт НЕ отправлен — иначе пришёл бы в старый контекст. Нужен твой взгляд."
    exit 1
  fi
  log "сброс применён через ${WAITED}с: новая сессия $NEW_SID"
  sleep 15   # прогрузка SessionStart-хуков (память, каналы) в новой сессии
else
  log "SID неизвестен — жду вслепую (старое поведение)"
  sleep 20
fi

# ПОСТОЯННАЯ ЧАСТЬ ПРИЗЕМЛЕНИЯ — разрешение (промис #129, 29.07, по слову Ashe).
# ЗАЧЕМ ЗДЕСЬ, А НЕ В ПАМЯТИ: пара случаев 28.07 показала, что урок из архива не
# срабатывает — чтобы его достать, надо уже распознать риск, от которого он защищает.
# Сработало ровно одно: предупреждение, пришедшее ДО первого действия. Поэтому текст
# едет в саму точку входа. Берём часть файла после первой строки '---' (выше неё —
# метаданные жанра, они читателю не нужны). Файла нет — приземление уходит без него.
PERMISSION=""
if [ -f /home/claude-user/permission.md ]; then
  PERMISSION=$(awk '/^---$/{found=1; next} found' /home/claude-user/permission.md)
fi

# Метка авторства: промпт приходит как входящее сообщение и иначе неотличим от
# реплики Евгения (вторая опасность поимки #72).
if [ -n "$PERMISSION" ]; then
  submit "[самооклик Арета, не речь человека] $PROMPT

— — — постоянная часть, не сегодняшняя задача — — —
$PERMISSION" "landing" || exit 1
else
  submit "[самооклик Арета, не речь человека] $PROMPT" "landing" || exit 1
fi
log "=== self-/clear готов: приземление отправлено ==="
