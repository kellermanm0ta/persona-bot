# persona-bot

Bot de Telegram **genérico** que conversa em personagem, em pt-BR, opcionalmente
fundamentado num contexto (PDFs) via **RAG híbrido** (BM25 + embeddings locais).
Cada instância roda **uma persona**; o que muda entre elas é a configuração —
ninguém precisa mexer no código: troca-se o prompt em `.opencode/agent/<persona>.md`
e a config no `docker-compose.yml`.

## Como funciona

```
telegram_bot.py -> runtime.py -> OpenRouter (openrouter-agent-sdk)
                       |               tool: consultar_contexto (se RAG_ENABLED)
                       v
                  ferramentas.py  (BM25 + vetores locais, fusão RRF)
                       ^
                  indexar.py  (pdftotext + fastembed)  <-  CONTEXT_DIR/*.pdf
```

- `config.py` lê tudo do ambiente (`AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, …).
- Se `RAG_ENABLED=false`, o bot é só persona (sem corpus, sem tool).
- `indexar.py` gera o índice BM25 e os embeddings a partir dos PDFs de `CONTEXT_DIR`.

## Layout

```
.opencode/agent/<persona>.md     a persona (fonte da verdade); `bot_name:` opcional
contexto/<persona>/*.pdf         contexto opcional; `titulos.json` mapeia arquivo -> titulo
.secrets/openrouter.env          chave da OpenRouter (compartilhada)
.secrets/<persona>/telegram.env  token do bot (um por persona)
data/<persona>/                  estado: sessions, log, corpus, embeddings
```

## Adicionar uma persona

1. `.opencode/agent/<persona>.md` (copie o molde de `buda.md`/`bukowski.md`; ajuste o
   `model:` e opcionalmente `bot_name:`).
2. (Opcional) PDFs em `contexto/<persona>/` + `contexto/<persona>/titulos.json`.
3. `.secrets/<persona>/telegram.env` com o token do bot (BotFather).
4. Um serviço no `docker-compose.yml` (copie o bloco `bukowski` e ajuste `AGENT`,
   `RAG_ENABLED`, `CONTEXT_DIR` e os volumes).

## Rodar com Docker (recomendado)

```bash
cp .secrets/openrouter.env.example .secrets/openrouter.env   # OPENROUTER_API_KEY
cp .secrets/telegram.env.example .secrets/<persona>/telegram.env
chmod 600 .secrets/openrouter.env .secrets/<persona>/telegram.env

docker compose up -d --build
docker compose logs -f bukowski
```

Na primeira subida de uma persona com RAG, o container baixa o modelo de embeddings
(~0,2 GB) e indexa o contexto; depois reaproveita `data/<persona>` e o cache.

## Rodar no host (sem Docker)

Requer `pdftotext` (poppler-utils) e a venv dedicada.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-bot.txt
./.venv/bin/python indexar.py            # só se RAG_ENABLED
nohup ./run_bot.sh >/dev/null 2>&1 </dev/null &
kill "$(cat data/<AGENT>/bot.pid)"       # parar (~25 s)
```

Comandos no chat: `/start`, `/help`, `/limpar`, `/id` e `/contexto` (só com RAG).

## Configuração (environment)

`AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, `STATE_DIR`, `RETRIEVAL` (`hybrid`|`bm25`),
`EMBED_MODEL`, `OPENROUTER_MODEL`, `MAX_HISTORY`, `INDEX_ON_START` — detalhes e defaults
em `AGENTS.md`.

## Aviso

Os PDFs em `contexto/` têm **direitos autorais** e **não** são versionados. Nunca
comite os PDFs nem um `corpus.json`/`embeddings.npy` pré-construído (contêm trechos).
