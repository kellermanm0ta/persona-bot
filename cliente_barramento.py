#!/usr/bin/env python3
"""Cliente do barramento HTTP entre personas.

Publica mensagens humanas e falas no barramento e consome convites via
long-poll. O cursor fica em STATE_DIR/barramento_cursor.json, para retomar apos
restart sem replay infinito.
"""
import json
import time
from typing import Callable

import requests

import config
import logsetup

log = logsetup.get_logger("cliente_barramento")

CURSOR = config.STATE_DIR / "barramento_cursor.json"


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.BARRAMENTO_TOKEN:
        h["Authorization"] = f"Bearer {config.BARRAMENTO_TOKEN}"
    return h


def _post(payload: dict, timeout=3) -> bool:
    if not config.BARRAMENTO_ENABLED:
        return False
    try:
        r = requests.post(
            f"{config.BARRAMENTO_URL}/eventos",
            json=payload,
            headers=_headers(),
            timeout=timeout,
        )
        if r.status_code >= 400:
            log.warning(
                "barramento POST %s -> %s %s",
                payload.get("tipo"),
                r.status_code,
                r.text[:200],
            )
            return False
        return True
    except Exception as e:
        log.warning("barramento indisponivel: %r", e)
        return False


def publicar_humano(chat_id: int, message_id: int, texto: str, origem_nome: str) -> bool:
    return _post(
        {
            "tipo": "humano",
            "origem": config.AGENT,
            "origem_nome": origem_nome,
            "chat_id": chat_id,
            "message_id": message_id,
            "texto": texto,
        },
        timeout=(2, 3),
    )


def publicar_fala(
    conversa_id: str,
    chat_id: int,
    indice: int,
    rodada: int,
    texto: str,
    message_id: int | None,
    reply_to: int | None,
    origem_nome: str,
) -> bool:
    return _post(
        {
            "tipo": "fala",
            "origem": config.AGENT,
            "autor_nome": origem_nome,
            "conversa_id": conversa_id,
            "chat_id": chat_id,
            "indice": indice,
            "rodada": rodada,
            "texto": texto,
            "message_id": message_id,
            "reply_to": reply_to,
        },
        timeout=(2, 4),
    )


def publicar_passou(
    conversa_id: str, chat_id: int, indice: int, rodada: int, motivo: str
) -> bool:
    return _post(
        {
            "tipo": "passou",
            "origem": config.AGENT,
            "conversa_id": conversa_id,
            "chat_id": chat_id,
            "indice": indice,
            "rodada": rodada,
            "motivo": motivo,
        },
        timeout=(2, 3),
    )


def _carregar_cursor() -> int:
    try:
        data = json.loads(CURSOR.read_text(encoding="utf-8"))
        return int(data.get("seq", 0))
    except Exception:
        return 0


def _salvar_cursor(seq: int) -> None:
    try:
        CURSOR.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seq": seq}), encoding="utf-8")
        tmp.replace(CURSOR)
    except Exception as e:
        log.warning("falha ao salvar cursor: %r", e)


def consumir(callback: Callable[[dict], None]) -> None:
    """Long-poll do hub; chama callback(convite) para cada convite ativo."""
    if not config.BARRAMENTO_ENABLED:
        return
    if not config.BARRAMENTO_TOKEN:
        log.error("BARRAMENTO_ENABLED sem BARRAMENTO_TOKEN; barramento desligado")
        return
    if not config.BARRAMENTO_CHAT_IDS:
        log.warning("BARRAMENTO_CHAT_IDS vazio; convites serao aceitos para qualquer chat")

    cursor = _carregar_cursor()
    log.info("barramento consumindo %s (cursor=%d)", config.BARRAMENTO_URL, cursor)
    espera = 3
    while True:
        try:
            r = requests.get(
                f"{config.BARRAMENTO_URL}/eventos",
                params={
                    "destinatario": config.AGENT,
                    "desde": cursor,
                    "timeout": config.BARRAMENTO_POLL_TIMEOUT,
                },
                headers=_headers(),
                timeout=(5, config.BARRAMENTO_POLL_TIMEOUT + 10),
            )
            if r.status_code == 401:
                log.error("barramento: token recusado")
                time.sleep(10)
                continue
            r.raise_for_status()
            data = r.json()
            espera = 3
            seq = int(data.get("seq", cursor))
            if seq < cursor:
                log.warning("cursor %d > seq %d do hub; reiniciando", cursor, seq)
                cursor = 0
                _salvar_cursor(cursor)
                continue
            eventos = data.get("eventos") or []
            for ev in eventos:
                try:
                    callback(ev)
                except Exception as e:
                    log.exception("erro processando convite: %r", e)
                cursor = max(cursor, int(ev.get("seq", cursor)))
                _salvar_cursor(cursor)
            if not eventos:
                cursor = seq
                _salvar_cursor(cursor)
        except Exception as e:
            log.warning("barramento GET falhou: %r", e)
            time.sleep(espera)
            espera = min(espera * 2, 30)