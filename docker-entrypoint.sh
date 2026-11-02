#!/usr/bin/env bash
set -euo pipefail
cd /app

# Indexa o contexto na primeira subida, se o RAG estiver ligado e ainda nao houver indice.
if [ "${RAG_ENABLED:-false}" = "true" ] && [ "${INDEX_ON_START:-true}" = "true" ]; then
  if [ ! -f /app/.state/corpus.json ]; then
    echo "indexando contexto (${CONTEXT_DIR:-contexto/$AGENT})..."
    /app/.venv/bin/python indexar.py || echo "aviso: indexacao falhou; seguindo sem indice"
  fi
fi

exec /app/.venv/bin/python telegram_bot.py
