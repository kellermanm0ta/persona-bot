#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Config da instancia (host). Em Docker isso vem do docker-compose.
export AGENT="${AGENT:-bukowski}"
export STATE_DIR="${STATE_DIR:-data/$AGENT}"
export CONTEXT_DIR="${CONTEXT_DIR:-contexto/$AGENT}"
export RAG_ENABLED="${RAG_ENABLED:-true}"

mkdir -p "$STATE_DIR"
PIDFILE="$STATE_DIR/bot.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "ja em execucao: pid $(cat "$PIDFILE")" >&2
  exit 0
fi

# Desanexa da sessao/terminal para sobreviver ao fechamento e a kills do grupo.
if [ "${_BOT_BG:-}" != "1" ] && command -v setsid >/dev/null 2>&1; then
  _BOT_BG=1 exec setsid --fork "$0" "$@"
fi

set -a
if [ -f ./.secrets/openrouter.env ]; then
  . ./.secrets/openrouter.env
fi
if [ -f "./.secrets/$AGENT/telegram.env" ]; then
  . "./.secrets/$AGENT/telegram.env"
elif [ -f ./.secrets/telegram.env ]; then
  . ./.secrets/telegram.env
fi
set +a
echo $$ > "$PIDFILE"
exec "$PWD/.venv/bin/python" telegram_bot.py >> "$STATE_DIR/bot.log" 2>&1
