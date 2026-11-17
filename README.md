# persona-bots

Bot de Telegram **genérico** que conversa em personagem, em pt-BR, opcionalmente
fundamentado num contexto (PDFs/TXTs) via **RAG híbrido** (BM25 + embeddings locais).
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
                  indexar.py  (pdftotext + fastembed)  <-  CONTEXT_DIR/*.pdf|*.txt
```

- `config.py` lê tudo do ambiente (`AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, …).
- Se `RAG_ENABLED=false`, o bot é só persona (sem corpus, sem tool).
- `indexar.py` gera o índice BM25 e os embeddings a partir dos PDFs/TXTs de `CONTEXT_DIR`.

## Comunicação entre agentes (ponte)

Bots do Telegram não leem mensagens de outros bots. Para duas personas conversarem
no mesmo grupo, um serviço interno (`barramento`, HTTP/JSON) coordena os turnos:

```
telegram_bot.py <-> cliente_barramento.py <-> barramento.py (rede agentes)
```

- o bot publica a mensagem humana no hub e **não** responde direto nos grupos
  configurados;
- o hub interpreta o pedido, convida um bot por vez e recebe a fala publicada;
- padrão: **1 rodada** (cada participante fala uma vez; 2 mensagens com 2 bots);
- `Pessoa e Bukowski interajam sobre o mar em 3 rodadas` → 3 rodadas × 2 bots = 6 mensagens;
- um único bot mencionado responde sozinho; os demais ficam calados (mesmo com
  `N rodadas`);
- sem menção e sem pedido explícito (`Oi`), também é 1 rodada com os bots do hub;
- uma nova mensagem humana durante um plano cancela o plano anterior;
- sem `BARRAMENTO_ENABLED`, ou com o hub fora do ar, o bot volta a responder direto.

Arquivos: `barramento.py` (hub) e `cliente_barramento.py` (cliente dos bots). A rede Docker `agentes`
é interna; o hub não publica portas nem acessa a internet. O log fica em
`data/barramento/eventos.jsonl` (JSONL: um objeto JSON por linha). Configuração detalhada
em `AGENTS.md`.

Para incluir outra persona na ponte: adicione `./.secrets/barramento.env`,
`BARRAMENTO_ENABLED=true`, `BARRAMENTO_URL=http://barramento:8080` e as redes
`default` + `agentes` ao serviço; inclua o nome em `BARRAMENTO_BOTS`/`BARRAMENTO_ORDEM`
do `barramento`. O grupo coordenado fica em `BARRAMENTO_CHAT_IDS`.

## Layout

```
.opencode/agent/<persona>.md     a persona (fonte da verdade); `bot_name:` opcional
contexto/<persona>/*.pdf|*.txt    contexto opcional; `titulos.json` mapeia arquivo -> titulo
.secrets/openrouter.env          chave da OpenRouter (compartilhada)
.secrets/<persona>/telegram.env  token do bot (um por persona)
.secrets/barramento.env          token compartilhado da ponte entre agentes
data/<persona>/                  estado: sessions, log, corpus, embeddings
data/barramento/eventos.jsonl    log JSONL do barramento entre agentes
```

## Adicionar uma persona

1. `.opencode/agent/<persona>.md` (copie o molde de `buda.md`/`bukowski.md`/`pessoa.md`/`ramanujan.md`;
   ajuste o `model:` e opcionalmente `bot_name:`).
2. (Opcional) PDFs ou `.txt` em `contexto/<persona>/` + `contexto/<persona>/titulos.json` (este
   arquivo é **local, não versionado** — o `contexto/` está no `.gitignore`). Para o acervo
   Pessoa, `coletar_arquivo_pessoa.py` gera esse material a partir de `arquivopessoa.net`.
3. `.secrets/<persona>/telegram.env` com o token do bot (BotFather).
4. Um serviço no `docker-compose.yml` (copie o bloco `pessoa` e ajuste `AGENT`,
   `RAG_ENABLED`, `CONTEXT_DIR` e os volumes). Para entrar na ponte, adicione
   `./.secrets/barramento.env`, `BARRAMENTO_ENABLED/URL` e as redes `default`+`agentes`.

## Rodar com Docker (recomendado)

```bash
cp .secrets/openrouter.env.example .secrets/openrouter.env   # OPENROUTER_API_KEY
cp .secrets/telegram.env.example .secrets/<persona>/telegram.env
chmod 600 .secrets/openrouter.env .secrets/<persona>/telegram.env

docker compose up -d --build
docker compose logs -f bukowski
docker compose logs -f barramento   # se usar a ponte
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

Comandos no chat: `/start`, `/help`, `/limpar`, `/id` (mostra user id e chat id)
e `/contexto` (só com RAG).

Para a ponte no host, rode `barramento.py` em um terminal e exporte `BARRAMENTO_*`
nos bots; sem o hub, eles respondem direto.

## Configuração (environment)

`AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, `STATE_DIR`, `RETRIEVAL` (`hybrid`|`bm25`),
`EMBED_MODEL`, `OPENROUTER_MODEL`, `MAX_HISTORY`, `INDEX_ON_START`,
`BARRAMENTO_ENABLED`, `BARRAMENTO_URL`, `BARRAMENTO_CHAT_IDS` — detalhes e defaults
em `AGENTS.md`.

## Aviso

Os PDFs/TXTs em `contexto/` têm **direitos autorais** e **não** são versionados. Nunca
comite os arquivos nem um `corpus.json`/`embeddings.npy` pré-construído (contêm trechos).
