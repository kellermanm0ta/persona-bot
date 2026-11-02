#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .state

if [ -f .state/bot.pid ] && kill -0 "$(cat .state/bot.pid)" 2>/dev/null; then
  echo "ja em execucao: pid $(cat .state/bot.pid)" >&2
  exit 0
fi

# Desanexa da sessao/terminal para sobreviver ao fechamento e a kills do grupo.
if [ "${_BOT_BG:-}" != "1" ] && command -v setsid >/dev/null 2>&1; then
  _BOT_BG=1 exec setsid --fork "$0" "$@"
fi

set -a
. ./.secrets/telegram.env
if [ -f ./.secrets/openrouter.env ]; then
  . ./.secrets/openrouter.env
fi
set +a
echo $$ > .state/bot.pid
exec "$PWD/.venv/bin/python" telegram_bot.py >> .state/bot.log 2>&1
