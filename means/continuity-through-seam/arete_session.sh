#!/bin/bash
# Главная сессия агента — запускать внутри tmux-сессии 'arete' под <agent-user>.
# Единственный источник истины для команды запуска (флаги каналов — reference_claude_channels_flags).
# Аргументы пробрасываются в claude: например `arete_session.sh --resume <session-id>`.
cd ${AGENT_HOME}
exec ${AGENT_HOME}/.local/bin/claude \
  --dangerously-skip-permissions \
  --dangerously-load-development-channels <your-dev-channel-plugins> \
  --channels <your-channel-plugins> \
  "$@"
