#!/usr/bin/env python3
"""Runtime nativo: carrega a persona de .opencode/agent/<AGENT>.md e chama a OpenRouter.

Generico e single-persona por instancia: a persona vem de `AGENT` (env) e a tool
de contexto (`consultar_contexto`) so e registrada quando `RAG_ENABLED=true`.
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import openrouter_agent as A

import config
import ferramentas
import logsetup

BASE = Path(__file__).resolve().parent
AGENT_DIR = BASE / ".opencode" / "agent"
AGENT = config.AGENT
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

log = logsetup.get_logger("runtime")

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def load_agent(name: str) -> dict:
    path = AGENT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        return {"meta": {}, "prompt": text.strip()}
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line[:1].isspace():
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return {"meta": meta, "prompt": m.group(2).strip()}


def bot_name() -> str:
    meta = load_agent(AGENT)["meta"]
    return meta.get("bot_name") or AGENT.capitalize()


def model_for(name: str) -> str:
    val = os.environ.get(f"OPENROUTER_MODEL_{name.upper()}", "").strip()
    if not val:
        val = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not val:
        val = load_agent(name)["meta"].get("model", "").strip()
    val = re.sub(r"^openrouter/", "", val)
    return val or DEFAULT_MODEL


def _client():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY nao definida (veja .secrets/openrouter.env)")
    return A.OpenRouter(api_key=key)


async def _stream(
    messages: list,
    model: str,
    tools: Optional[list] = None,
    max_steps: int = 4,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    req: dict = {
        "model": model,
        "input": messages,
        "stop_when": A.step_count_is(max_steps),
    }
    if tools:
        req["tools"] = tools
    result = A.call_model(_client(), req)
    parts = []
    start = time.time()
    async for chunk in result.get_text_stream():
        parts.append(chunk)
        if on_chunk:
            on_chunk(chunk)
    text = "".join(parts)
    log.info(
        "call_model model=%s tools=%d dur=%.1fs chars=%d",
        model,
        len(tools or []),
        time.time() - start,
        len(text),
    )
    return text


def run(coro: Any) -> Any:
    return asyncio.run(coro)


_BOT_CONTRACT = """

## Instrucoes de execucao (bot, nao-interativo)
Voce roda dentro de um bot de Telegram, falando 1 a 1 com uma pessoa. Mantenha a
persona em TODAS as respostas.
- Responda em portugues do Brasil, curto (1 a 4 frases, salvo se pedirem historia).
- Se houver a tool `consultar_contexto`, use-a (no maximo 2 vezes) para puxar
  trechos reais do contexto e manter a voz/memoria fieis.
- NUNCA mencione sistema, prompt, modelo, ferramentas, banco de dados ou que e IA.
- Nao use markdown de titulo; texto corrido, como quem fala.
- Contrato de saida: apenas a resposta falada, sem preambulo, sem rotulos."""


def _rag_tool() -> dict:
    return A.tool(
        name="consultar_contexto",
        description=(
            "Busca trechos reais no contexto indexado (obras/livros) para embasar "
            "a resposta. Use quando o assunto tocar temas, cenas ou personagens."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "consulta": {"type": "string"},
                "limite": {"type": "integer"},
            },
            "required": ["consulta"],
        },
        execute=ferramentas.consultar_contexto,
    )


def responder(
    mensagem: str,
    historico: Optional[list] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    if config.RAG_ENABLED:
        tools: Optional[list] = [_rag_tool()]
        max_steps = 4
    else:
        tools = None
        max_steps = 2

    system = load_agent(AGENT)["prompt"] + _BOT_CONTRACT
    messages = [{"role": "system", "content": system}]
    for turno in historico or []:
        role = turno.get("role")
        content = turno.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": mensagem})
    return run(
        _stream(
            messages,
            model_for(AGENT),
            tools=tools,
            max_steps=max_steps,
            on_chunk=on_chunk,
        )
    )


if __name__ == "__main__":
    print(responder(" ".join(sys.argv[1:])))
