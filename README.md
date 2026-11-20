# persona-bots

Bot de Telegram **genérico** que conversa em personagem, em pt-BR, opcionalmente
fundamentado num contexto (PDFs/TXTs) via **RAG híbrido** (BM25 + embeddings). Cada
instância roda **uma persona**; o que muda entre elas é a configuração — ninguém
precisa mexer no código: troca-se o prompt em `.opencode/agent/<persona>.md` e a
config no `docker-compose.yml`.

## Como funciona

```
telegram_bot.py -> runtime.py -> OpenRouter (openrouter-agent-sdk)
                       |               tool: consultar_contexto (se RAG_ENABLED)
                       v
                  ferramentas.py  (BM25 + vetores, fusão RRF; fallback BM25)
                       ^
                  indexar.py  (pdftotext + embeddings)  <-  CONTEXT_DIR/*.pdf|*.txt
```

- `config.py` lê tudo do ambiente (`AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, …).
- Se `RAG_ENABLED=false`, o bot é só persona (sem corpus, sem tool).
- `indexar.py` gera o índice BM25 e os embeddings a partir dos PDFs/TXTs de `CONTEXT_DIR`.
- `RETRIEVAL` escolhe o backend: `bm25` (lexical), `hybrid` (embeddings locais via
  `fastembed`/ONNX) ou `hybrid_api` (embeddings pela OpenRouter — só a query em runtime).

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

## Deploy (GitHub Actions → GHCR → VPS)

Pipeline: `push main` → **ci** valida → **publish** publica
`ghcr.io/kellermanm0ta/persona-bot:{latest,sha-xxxx}` → **deploy** puxa e sobe na VPS
(`inter-server-vps`) por SSH. O pacote é **público**, então a VPS não autentica no
registry. Serviços publicados: **bukowski**, **pessoa** e **barramento** (o hub da
ponte); buda/ramanujan continuam só no `docker-compose.yml` de desenvolvimento.

RAG em **híbrido com embeddings via API** (`RETRIEVAL=hybrid_api`): os trechos vêm do
`embeddings.npy` local e só a **query** é vetorizada pela OpenRouter — sem
`fastembed`/ONNX no runtime (~100 MB por bot, contra ~690 MB no híbrido local). O
modelo é o `EMBED_MODEL` (default `baai/bge-m3`); o `indexar.py` gera os vetores por
API, então o índice (`corpus.json` + `embeddings.npy` + `index_meta.json`) é
provisionado na VPS. Se a API falhar, a busca cai no BM25.

Segredos (GitHub → Settings → Secrets and variables → Actions):

| Secret | Uso |
|---|---|
| `BUKOWSKI_TELEGRAM_BOT_TOKEN`, `PESSOA_TELEGRAM_BOT_TOKEN` | `.secrets/<persona>/telegram.env` |
| `TELEGRAM_ALLOWED_USER_IDS` | idem (lista compartilhada) |
| `OPENROUTER_API_KEY` | chave **exclusiva** do projeto → `.secrets/openrouter.env` |
| `BARRAMENTO_TOKEN`, `BARRAMENTO_CHAT_IDS` | `.secrets/barramento.env` |
| `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` | acesso SSH (`VPS_PORT` opcional) |

### Setup da VPS (uma vez)

Pré-requisitos: usuário no grupo `docker`, Docker Engine + compose v2, chave SSH. O
`contexto/` (copyright) e o índice **não** vão pela CI — copie do host uma vez:

```bash
ssh inter-server-vps 'mkdir -p ~/persona-bots/{data/bukowski,data/pessoa,data/barramento,.secrets/bukowski,.secrets/pessoa,contexto/{bukowski,pessoa}}'

rsync -a contexto/bukowski/ inter-server-vps:~/persona-bots/contexto/bukowski/
rsync -a contexto/pessoa/   inter-server-vps:~/persona-bots/contexto/pessoa/
scp data/bukowski/{corpus.json,embeddings.npy,index_meta.json} \
    inter-server-vps:~/persona-bots/data/bukowski/
scp data/pessoa/{corpus.json,embeddings.npy,index_meta.json} \
    inter-server-vps:~/persona-bots/data/pessoa/
```

Para regenerar o índice localmente (sem ONNX; usa a API e custa ~centavos):

```bash
RETRIEVAL=hybrid_api EMBED_MODEL=baai/bge-m3 \
  AGENT=bukowski STATE_DIR=data/bukowski CONTEXT_DIR=contexto/bukowski \
  ./.venv/bin/python indexar.py
```

Ao trocar de `EMBED_MODEL`, **re-indexe e reenvie** os dois arquivos — vetores de
modelos diferentes não são comparáveis (o runtime avisa e cai no BM25 se
`index_meta.json` não bater).

Primeiro deploy: rode o pipeline. Se o `pull` na VPS falhar com `401`, torne o pacote
público (GitHub → Packages → persona-bot → Package settings).

Rollback: Actions → **deploy** → Run workflow, informando `tag` (ex: `sha-ab12cd3`).

### Rotação de chave (OpenRouter)

O `.secrets` na VPS é cópia derivada dos GitHub Secrets — o deploy o reescreve a cada
run. Rotacione pelo secret, não editando o arquivo na VPS:

```bash
gh secret set OPENROUTER_API_KEY -R kellermanm0ta/persona-bot   # cole o valor puro
gh workflow run deploy.yml -R kellermanm0ta/persona-bot
```

O passo **Validar segredos** imprime só o fingerprint (`sha256` curto) e aborta antes do
SSH se a chave vier vazia, como caminho (`/...`, `~/...`) ou curta demais.

> ⚠️ Nunca rode a mesma persona no host **e** na VPS com o mesmo token: o Telegram
> devolve `409 Conflict`. Pare uma instância antes de subir a outra.

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

`AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, `STATE_DIR`, `RETRIEVAL`
(`bm25`|`hybrid`|`hybrid_api`), `EMBED_MODEL` (fastembed em `hybrid`, id OpenRouter em
`hybrid_api`), `EMBED_BATCH`, `EMBED_DIM`, `OPENROUTER_MODEL`, `MAX_HISTORY`,
`INDEX_ON_START`, `BARRAMENTO_ENABLED`, `BARRAMENTO_URL`, `BARRAMENTO_CHAT_IDS` —
detalhes e defaults em `AGENTS.md`.

## Aviso

Os PDFs/TXTs em `contexto/` têm **direitos autorais** e **não** são versionados. Nunca
comite os arquivos nem um `corpus.json`/`embeddings.npy` pré-construído (contêm trechos).
