# persona-bot

Bot de Telegram que conversa **em personagem como Charles Bukowski**, em pt-BR,
fundamentado em duas obras (*Misto Quente* e *Mulheres*) via RAG híbrido. Roda
nativo no host (sem Docker, sem opencode em runtime), em Python.

## Arquitetura

```
telegram_bot.py  ->  runtime.py  ->  OpenRouter (openrouter-agent-sdk)
                        |                tool: consultar_obras
                        v
                   ferramentas.py  (BM25 + vetores locais, fusão RRF)
                        ^
                   indexar.py  (pdftotext + fastembed)  <-  contexto/*.pdf
```

- `runtime.py` lê a persona de `.opencode/agent/bukowski.md` e chama a OpenRouter.
- `ferramentas.py` expõe a única tool do agente, `consultar_obras`, com busca
  híbrida: BM25 (Python puro) + similaridade semântica (embeddings ONNX locais,
  via `fastembed`), combinadas por RRF; cai no BM25 puro se não houver vetores.
- `indexar.py` extrai os PDFs, limpa boilerplate, gera o índice BM25
  (`.state/corpus.json`) e os embeddings (`.state/embeddings.npy`).

## Setup

Requer `pdftotext` (poppler-utils) no PATH.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-bot.txt

# segredos
cp .secrets/openrouter.env.example .secrets/openrouter.env   # preencha OPENROUTER_API_KEY
cp .secrets/telegram.env.example .secrets/telegram.env       # preencha TELEGRAM_BOT_TOKEN
chmod 600 .secrets/*.env

# coloque as obras (PDF com camada de texto) em contexto/ e indexe
./.venv/bin/python indexar.py
```

A primeira indexação baixa o modelo de embeddings (~0,2 GB). Troque de modelo com
`BUKOWSKI_EMBED_MODEL`.

## Rodar

```bash
nohup ./run_bot.sh >/dev/null 2>&1 </dev/null &
kill "$(cat .state/bot.pid)"     # parar (pode levar ~25 s)
```

Comandos no chat: `/start`, `/help`, `/limpar`, `/obras`, `/id`.
Logs em `.state/bot.log`.

## Verificação (sem test suite)

```bash
./.venv/bin/python indexar.py
./.venv/bin/python ferramentas.py "corrida de cavalos"
./.venv/bin/python runtime.py "E aí, velho. O que você faria?"
```

Detalhes de operação e armadilhas em `AGENTS.md`.

## Aviso

As obras em `contexto/` têm **direitos autorais** e **não** são versionadas
(`.gitignore`). Nunca comite os PDFs nem um `.state/corpus.json` pré-construído.
