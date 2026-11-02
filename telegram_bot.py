#!/usr/bin/env python3
"""Bot do Telegram: conversa em personagem com o agente "Bukowski".

Long-polling nativo (sem Docker, sem opencode). O LLM e chamado direto na
OpenRouter via runtime.responder(); a memoria de conversa fica em
.state/sessions.json.
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


def api(method, **params):
    resp = requests.post(f"{API}/{method}", data=params, timeout=90)
    resp.raise_for_status()
    return resp.json()


def send(chat_id, text, reply_to=None):
    for chunk in _chunks(text):
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            reply_to = None
        api("sendMessage", **params)


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
        log(f"resposta erro: {e!r}")
        send(chat_id, f"Tive um problema ao responder: {e}", reply_to=message_id)
        _busy.discard(chat_id)
        return
    add_turn(chat_id, "user", texto)
    add_turn(chat_id, "assistant", resposta)
    send(chat_id, resposta, reply_to=message_id)
    log(f"chat {chat_id}: {texto[:80]!r} -> {resposta[:120]!r}")
    _busy.discard(chat_id)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    if ALLOWED and user_id not in ALLOWED:
        send(chat_id, "Acesso nao autorizado.", reply_to=msg["message_id"])
        return
    if text in ("/start", "/help"):
        send(chat_id, HELP, reply_to=msg["message_id"])
        return
    if text == "/id":
        send(chat_id, f"user id: {user_id}", reply_to=msg["message_id"])
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
            log(f"getUpdates falhou: {e}")
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
                log(f"erro processando update: {e!r}")
                try:
                    send(msg["chat"]["id"], f"Erro inesperado: {e}")
                except Exception:
                    pass

    log("encerrado")


if __name__ == "__main__":
    main()
