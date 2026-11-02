FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-bot.txt ./
RUN python -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/.venv/bin/pip install --no-cache-dir -r requirements-bot.txt

COPY config.py logsetup.py ferramentas.py indexar.py runtime.py telegram_bot.py ./
COPY .opencode/agent ./.opencode/agent
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh \
 && mkdir -p /app/contexto /app/.state /models \
 && chown -R 1000:1000 /app /models

ENTRYPOINT ["/app/docker-entrypoint.sh"]
