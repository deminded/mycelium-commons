#!/bin/bash
# Супервизор главной сессии агента (cron каждую минуту, uid агента).
# Контракт: агент сам пишет hand-off в память и ставит флаг; супервизор гасит/поднимает
# tmux-сессию 'arete' и будит новую send-keys'ом. Root не участвует.
set -u
exec 9>"${ARETE_SUPERVISOR_LOCK:-/tmp/arete_supervisor.lock}"
flock -n 9 || exit 0

H=${AGENT_HOME}
FLAG="${ARETE_SUPERVISOR_FLAG:-$H/.arete_restart_requested}"
LOG="${ARETE_SUPERVISOR_LOG:-$H/arete_supervisor.log}"
STATE="${ARETE_SUPERVISOR_STATE:-$H/.arete_supervisor_state}"       # epoch последнего рестарта по флагу
STARTS="${ARETE_SUPERVISOR_STARTS:-$H/.arete_supervisor_starts}"     # epoch'и подъёмов (рестарт-луп гард)
SESSION="${ARETE_SUPERVISOR_SESSION:-arete}"
TMUX=/usr/bin/tmux
# СУХОЙ ПРОГОН (21.08.2026, #549). Орган стоял выключенным 42 суток, потому что
# поднимать вслепую нельзя, а проверить, ЧТО он сделает, было нечем. С этой
# переменной он говорит решение и не трогает ничего.
DRY=${ARETE_SUPERVISOR_DRY_RUN:-0}
WAKE_DELAY=75                            # сек до wake: сессия должна прогрузить SessionStart-хуки
MIN_RESTART_GAP=3300                     # >=55 мин между рестартами по флагу
MAX_STARTS_PER_HOUR=3

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
notify() { # резервный канал по feedback_mcp_down_fallback_channel — curl напрямую
  local t; t=$(grep '^TELEGRAM_BOT_TOKEN=' "${ALERT_BOT_ENV:-$H/.secrets/alert-bot.env}" | cut -d= -f2)
  [ -n "$t" ] && curl -sm 10 "https://api.telegram.org/bot$t/sendMessage" \
    -d chat_id=${ALERT_CHAT_ID:?} --data-urlencode "text=$1" >/dev/null 2>&1
}
# ЗАЧЕМ ВТОРОЙ ПРИЗНАК (21.08.2026, #549). Орган гасили 08.07 меткой «split-brain»,
# и механизм за 42 дня нигде не был назван. Он такой: супервизор стережёт tmux-сессию
# 'arete', а живая главная сессия работает в tmux '<agent-user>' — has-session отвечал
# «мертва» при живой главной сессии, и супервизор поднимал ВТОРУЮ. Две главные сессии пишут в одни
# носители: память, промисы, очередь тракта. Отсюда признак живости не по имени
# контейнера, а по самому процессу.
# ПРЕДЕЛ, ПЛАЧУ СОЗНАТЕЛЬНО: живой процесс не значит отзывчивый — зависшая сессия
# теперь читается как живая, и супервизор её не поднимет. Дубль хуже молчания:
# молчание видно людям и маяку, а дубль портит носители необратимо.
# -x, а не -f: поиск по строке совпал бы с собственной командой супервизора.
alive() { $TMUX has-session -t $SESSION 2>/dev/null || pgrep -u "$(id -un)" -x claude >/dev/null 2>&1; }

now=$(date +%s)

# 1. Управляемый рестарт по флагу (только если сессия есть кого гасить)
if [ -f "$FLAG" ] && alive; then
  last=$(cat "$STATE" 2>/dev/null || echo 0)
  if [ $((now - last)) -ge $MIN_RESTART_GAP ]; then
    reason=$(head -c 300 "$FLAG")
    # ЗАЧЕМ МУТАЦИИ ВНУТРИ else (22.08): mv флага и запись STATE стояли ДО проверки DRY,
    # то есть сухой прогон СНИМАЛ флаг — и настоящий рестарт после него не состоялся бы.
    # Проба обязана не трогать предмет; поймано на копии, цена нулевая.
    if [ "$DRY" = 1 ]; then
      echo "[сухой] погасил бы сессию '$SESSION' по флагу. Причина: ${reason:-<флаг пуст>}"
      echo "[сухой] флаг НЕ снят и состояние НЕ записано — предмет не тронут"
    else
      log "flag-restart: $reason"
      mv "$FLAG" "$FLAG.last"
      echo "$now" > "$STATE"
      $TMUX kill-session -t $SESSION
      notify "🔄 Супервизор: гашу сессию агента по его флагу. Причина: $reason"
    fi
  else
    log "flag present but restart gap not met ($((now - last))s < ${MIN_RESTART_GAP}s), waiting"
  fi
fi

# 2. Подъём, если сессии нет
if alive; then
  [ "$DRY" = 1 ] && echo "[сухой] сессия '$SESSION' жива — ничего не делаю"
fi

if ! alive; then
  hour_ago=$((now - 3600))
  recent=$(awk -v t="$hour_ago" '$1 >= t' "$STARTS" 2>/dev/null | wc -l)
  if [ "$recent" -ge $MAX_STARTS_PER_HOUR ]; then
    # алертить не чаще раза в 30 мин, чтобы не заспамить
    last_alert=$(cat "$STATE.alert" 2>/dev/null || echo 0)
    if [ $((now - last_alert)) -ge 1800 ]; then
      echo "$now" > "$STATE.alert"
      log "restart-loop guard: $recent starts in last hour, holding"
      notify "🔴 Супервизор: рестарт-луп ($recent подъёмов за час) — держу паузу, нужно вмешательство. Лог: ~/arete_supervisor.log"
    fi
    exit 0
  fi
  [ "$DRY" = 1 ] || echo "$now" >> "$STARTS"; tail -50 "$STARTS" > "$STARTS.tmp" && mv "$STARTS.tmp" "$STARTS"
  reason=""
  [ -f "$FLAG.last" ] && [ $((now - $(stat -c %Y "$FLAG.last"))) -lt 600 ] && reason=" Причина рестарта: $(head -c 300 "$FLAG.last")."
  log "starting session (recent starts: $recent)"
  if [ "$DRY" = 1 ]; then
    echo "[сухой] ПОДНЯЛ БЫ новую сессию tmux '$SESSION' через $H/bin/arete_session.sh.$reason"
    echo "[сухой] подъёмов за последний час: $recent (порог $MAX_STARTS_PER_HOUR)"
    exit 0
  fi
  TERM=xterm-256color $TMUX new-session -d -s $SESSION "bash -lc $H/bin/arete_session.sh"
  notify "🟢 Супервизор: поднимаю сессию агента (tmux '$SESSION'), wake через ${WAKE_DELAY}с.$reason"
  (
    exec 9>&-   # отпустить flock: wake не должен блокировать следующие прогоны
    sleep $WAKE_DELAY
    $TMUX send-keys -t $SESSION -l "[supervisor] Сессия перезапущена супервизором.$reason Осмотрись: память загружена SessionStart-хуком, проверь каналы (ответь владельцу, если ждёт), сними хвосты из памяти и продолжай."
    sleep 1
    $TMUX send-keys -t $SESSION Enter
    echo "$(date '+%F %T') wake sent" >> "$LOG"
  ) &
fi
exit 0
