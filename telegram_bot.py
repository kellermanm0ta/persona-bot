#!/usr/bin/env python3
"""Bot do Telegram: conversa em personagem com o agente definido por AGENT.

Long-polling nativo. O LLM e chamado direto na OpenRouter via runtime.responder();
a memoria de conversa fica em STATE_DIR/sessions.json.
"""
import json
import os
import re
import signal
import sys
import threading
import time

import requests

import config
import logsetup
import cliente_barramento
import runtime

log = logsetup.get_logger("bot").info

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
    if x.strip().isdigit()
}
LONG_POLL = 25
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = config.STATE_DIR
STATE_FILE = STATE_DIR / "sessions.json"
MAX_HISTORY = config.MAX_HISTORY

_running = True
_state_lock = threading.Lock()
_sessions: dict = {}
_busy: set = set()


def build_help() -> str:
    cmds = ["  /limpar   - esquece a conversa."]
    if config.RAG_ENABLED:
        cmds.append("  /contexto - lista as fontes indexadas.")
    cmds.append("  /id       - mostra seu user id.")
    return f"Sou {runtime.bot_name()}. Manda o que quiser.\n\n" + "\n".join(cmds)


HELP = build_help()


def _redact(msg):
    """Remove o token do bot de qualquer mensagem/erro antes de logar ou enviar.

    Excecoes do requests incluem a URL completa, que contem o token em
    https://api.telegram.org/bot<TOKEN>/<method>.
    """
    text = str(msg)
    return text.replace(TOKEN, "***") if TOKEN else text


def api(method, **params):
    resp = requests.post(f"{API}/{method}", data=params, timeout=90)
    resp.raise_for_status()
    return resp.json()


def send(chat_id, text, reply_to=None):
    ultimo_id = None
    for chunk in _chunks(text):
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            reply_to = None
        resp = api("sendMessage", **params)
        ultimo_id = (resp.get("result") or {}).get("message_id", ultimo_id)
    return ultimo_id


def typing(chat_id):
    try:
        api("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass


def _chunks(text, limit=4000):
    text = text or ""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# Memoria por chat
# ---------------------------------------------------------------------------

def load_state():
    global _sessions
    try:
        if STATE_FILE.exists():
            _sessions = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"falha ao ler estado: {e!r}")
        _sessions = {}


def save_state():
    try:
        STATE_DIR.mkdir(exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_sessions, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log(f"falha ao salvar estado: {e!r}")


def get_history(chat_id):
    with _state_lock:
        return list(_sessions.get(str(chat_id), []))


def add_turn(chat_id, role, content):
    with _state_lock:
        hist = _sessions.setdefault(str(chat_id), [])
        hist.append({"role": role, "content": content})
        if len(hist) > MAX_HISTORY:
            del hist[:-MAX_HISTORY]
        save_state()


def clear_history(chat_id):
    with _state_lock:
        _sessions[str(chat_id)] = []
        save_state()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def responder_worker(chat_id, texto, message_id):
    _busy.add(chat_id)
    typing(chat_id)
    try:
        resposta = runtime.responder(texto, historico=get_history(chat_id))
        resposta = (resposta or "").strip() or "..."
    except Exception as e:
        log(f"resposta erro: {_redact(repr(e))}")
        send(chat_id, f"Tive um problema ao responder: {_redact(e)}", reply_to=message_id)
        _busy.discard(chat_id)
        return
    add_turn(chat_id, "user", texto)
    add_turn(chat_id, "assistant", resposta)
    send(chat_id, resposta, reply_to=message_id)
    log(f"chat {chat_id}: {texto[:80]!r} -> {resposta[:120]!r}")
    _busy.discard(chat_id)


def _passou(texto):
    return (
        re.fullmatch(r"\s*\[?\s*PASSAR\s*\]?\s*[.!]?\s*", texto or "", re.I)
        is not None
    )


def handle_convite(evento):
    """Responde a um convite do barramento (conversa coordenada no grupo)."""
    if evento.get("tipo") != "convite":
        return
    try:
        chat_id = int(evento.get("chat_id"))
    except Exception:
        log(f"barramento: convite sem chat_id valido: {evento!r}")
        return
    if config.BARRAMENTO_CHAT_IDS and chat_id not in config.BARRAMENTO_CHAT_IDS:
        log(f"barramento: chat {chat_id} nao permitido")
        return

    conversa_id = evento.get("conversa_id") or "?"
    try:
        indice = int(evento.get("indice") or 0)
        rodada = int(evento.get("rodada") or 1)
        max_mensagens = int(evento.get("max_mensagens") or 1)
    except Exception:
        log(f"barramento: convite invalido: {evento!r}")
        return
    permitir_passar = bool(evento.get("permitir_passar"))
    prompt = (evento.get("prompt") or "").strip()
    transcript = evento.get("transcript") or []
    participantes = evento.get("participantes") or []
    outros = ", ".join(p for p in participantes if p != config.AGENT) or "o grupo"

    for _ in range(60):
        if chat_id not in _busy:
            break
        time.sleep(0.5)
    if chat_id in _busy:
        log(f"barramento: chat {chat_id} ocupado; passando")
        cliente_barramento.publicar_passou(conversa_id, chat_id, indice, rodada, "ocupado")
        return

    _busy.add(chat_id)
    try:
        typing(chat_id)
        linhas = [f"Pedido do usuario: {prompt}", ""]
        if transcript:
            linhas.append("Falas ate agora:")
            for fala in transcript[-10:]:
                linhas.append(
                    f"- {fala.get('autor_nome') or fala.get('autor') or '?'}: "
                    f"{fala.get('texto', '')}"
                )
            linhas.append("")
        linhas.append(
            f"Sua vez, mensagem {indice + 1} de {max_mensagens} (rodada {rodada})."
        )
        mensagem = "\n".join(linhas)
        instrucao = (
            "## Conversa coordenada no grupo\n"
            f"Voce esta no grupo do Telegram com {outros}. "
            f"Esta e a mensagem {indice + 1} de {max_mensagens} (rodada {rodada}). "
            "Responda em 1 a 3 frases, em personagem, dirigindo-se ao grupo. "
            "Nao mencione o barramento, o sistema, o prompt, a ferramenta ou que foi convidado. "
        )
        if permitir_passar:
            instrucao += "Se nao tiver nada a acrescentar, responda exatamente [PASSAR]."
        else:
            instrucao += "Nao use [PASSAR]; responda necessariamente."

        resposta = (
            runtime.responder(
                mensagem, historico=get_history(chat_id), instrucao_extra=instrucao
            )
            or ""
        ).strip()
        if not resposta or (permitir_passar and _passou(resposta)):
            log(f"barramento: {config.AGENT} passou em {conversa_id}#{indice}")
            cliente_barramento.publicar_passou(conversa_id, chat_id, indice, rodada, "passou")
            return

        reply_to = evento.get("reply_to")
        try:
            message_id = send(chat_id, resposta, reply_to=reply_to)
        except Exception:
            message_id = send(chat_id, resposta)
        cliente_barramento.publicar_fala(
            conversa_id,
            chat_id,
            indice,
            rodada,
            resposta,
            message_id,
            reply_to,
            runtime.bot_name(),
        )
        if transcript:
            ultima = transcript[-1]
            add_turn(
                chat_id,
                "user",
                f"[{ultima.get('autor_nome') or ultima.get('autor') or 'grupo'} "
                f"no grupo]: {ultima.get('texto', '')}",
            )
        else:
            add_turn(chat_id, "user", f"[Grupo] {prompt}")
        add_turn(chat_id, "assistant", resposta)
        log(
            f"barramento: {config.AGENT} falou em {conversa_id}#{indice}: "
            f"{resposta[:120]!r}"
        )
    except Exception as e:
        log(f"barramento: erro no convite {conversa_id}#{indice}: {_redact(repr(e))}")
        cliente_barramento.publicar_passou(conversa_id, chat_id, indice, rodada, "erro")
    finally:
        _busy.discard(chat_id)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    remetente = msg.get("from", {}) or {}
    user_id = remetente.get("id")
    text = (msg.get("text") or "").strip()

    if remetente.get("is_bot"):
        return
    if ALLOWED and user_id not in ALLOWED:
        send(chat_id, "Acesso nao autorizado.", reply_to=msg["message_id"])
        return
    if text in ("/start", "/help"):
        send(chat_id, HELP, reply_to=msg["message_id"])
        return
    if text == "/id":
        send(
            chat_id,
            f"user id: {user_id}\nchat id: {chat_id}",
            reply_to=msg["message_id"],
        )
        return
    if text == "/limpar":
        clear_history(chat_id)
        send(chat_id, "Historico apagado. Comeca de novo.", reply_to=msg["message_id"])
        return
    if text == "/contexto" and config.RAG_ENABLED:
        fontes = runtime.ferramentas.listar_contexto()
        if not fontes:
            send(chat_id, "Nenhuma fonte indexada ainda.", reply_to=msg["message_id"])
            return
        linhas = "\n".join(f"- {o['nome']} ({o['arquivo']})" for o in fontes)
        send(chat_id, f"Fontes indexadas:\n{linhas}", reply_to=msg["message_id"])
        return
    if not text:
        return
    if text.startswith("/"):
        return

    if config.BARRAMENTO_ENABLED and chat_id in config.BARRAMENTO_CHAT_IDS:
        if cliente_barramento.publicar_humano(
            chat_id, msg["message_id"], text, runtime.bot_name()
        ):
            log(f"barramento: humano em {chat_id} msg {msg['message_id']} -> hub")
            return
        log("barramento indisponivel; respondendo direto")

    if chat_id in _busy:
        send(chat_id, "Ainda estou respondendo a mensagem anterior.", reply_to=msg["message_id"])
        return

    threading.Thread(
        target=responder_worker, args=(chat_id, text, msg["message_id"]), daemon=True
    ).start()


def stop(signum, frame):
    global _running
    _running = False
    log(f"sinal {signum}: encerrando apos o poll atual...")


def main():
    if not TOKEN:
        log("ERRO: defina TELEGRAM_BOT_TOKEN (veja .secrets/telegram.env)")
        sys.exit(1)

    load_state()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if config.BARRAMENTO_ENABLED:
        if config.BARRAMENTO_TOKEN:
            threading.Thread(
                target=cliente_barramento.consumir, args=(handle_convite,), daemon=True
            ).start()
            log(
                "barramento ativo -> %s chats=%s"
                % (config.BARRAMENTO_URL, sorted(config.BARRAMENTO_CHAT_IDS) or "(todos)")
            )
        else:
            log("ERRO: BARRAMENTO_ENABLED sem BARRAMENTO_TOKEN; barramento desligado")

    modelo = runtime.model_for(config.AGENT)
    if config.RAG_ENABLED:
        fontes = ", ".join(o["nome"] for o in runtime.ferramentas.listar_contexto()) or "(sem indice)"
        log(
            f"{runtime.bot_name()} iniciado (agente={config.AGENT} modelo={modelo} "
            f"rag={config.RETRIEVAL} fontes={fontes})"
        )
    else:
        log(
            f"{runtime.bot_name()} iniciado (agente={config.AGENT} modelo={modelo} "
            f"rag=off)"
        )

    offset = 0
    while _running:
        try:
            resp = api(
                "getUpdates",
                timeout=LONG_POLL,
                offset=offset,
                allowed_updates='["message"]',
            )
        except requests.RequestException as e:
            log(f"getUpdates falhou: {_redact(e)}")
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue
            try:
                handle_message(msg)
            except Exception as e:
                log(f"erro processando update: {_redact(repr(e))}")
                try:
                    send(msg["chat"]["id"], f"Erro inesperado: {_redact(e)}")
                except Exception:
                    pass

    log("encerrado")


if __name__ == "__main__":
    main()
