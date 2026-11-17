#!/usr/bin/env python3
"""Barramento HTTP interno entre personas (rede Docker).

Mantem planos de conversa coordenados e um log JSONL. Os bots publicam
mensagens humanas e falas; o hub convida um bot por vez, na ordem do plano.
Sem dependencias externas (stdlib).
"""
import hmac
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import logsetup

log = logsetup.get_logger("servidor")

PORTA = int(os.environ.get("BARRAMENTO_PORTA", "8080") or "8080")
TOKEN = os.environ.get("BARRAMENTO_TOKEN", "").strip()
BOTS = [x.strip() for x in os.environ.get("BARRAMENTO_BOTS", "pessoa,bukowski").split(",") if x.strip()]
ORDEM = [x.strip() for x in os.environ.get("BARRAMENTO_ORDEM", ",".join(BOTS)).split(",") if x.strip()]
ORDEM = [x for x in ORDEM if x in BOTS] or BOTS
CHAT_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("BARRAMENTO_CHAT_IDS", ""))
    if x.strip().lstrip("-").isdigit()
}
TIMEOUT = float(os.environ.get("BARRAMENTO_TIMEOUT", "60") or "60")
MAX_MENSAGENS = int(os.environ.get("BARRAMENTO_MAX_MENSAGENS", "12") or "12")
RODADAS_PADRAO = int(os.environ.get("BARRAMENTO_RODADAS_PADRAO", "1") or "1")
STATE = Path(os.environ.get("BARRAMENTO_STATE", "/app/.state/eventos.jsonl"))

ALIASES = {
    "pessoa": [
        "pessoa",
        "fernando",
        "fernando pessoa",
        "pessoa_escritor_bot",
        "caeiro",
        "alberto caeiro",
        "ricardo reis",
        "alvaro de campos",
        "bernardo soares",
    ],
    "bukowski": [
        "bukowski",
        "charles",
        "charles bukowski",
        "charles_bukowski_escritor_bot",
    ],
    "buda": [
        "buda",
        "siddhartha",
        "gautama",
        "buda_mente_saudavel_bot",
    ],
    "ramanujan": [
        "ramanujan",
        "srinivasa",
        "srinivasa ramanujan",
        "srinivasa_matematica_bot",
    ],
}

_VERBO = re.compile(
    r"\b(interajam|interagir|interacao|conversem|conversar|debate|debater|"
    r"discutam|discutir|dialoguem|dialogar)\b"
)
_CONTAGEM = re.compile(r"(\d+)\s*(rodadas?|turnos?|mensagens?)\b")


def _norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.replace("@", " ")
    texto = re.sub(r"[^\w\s]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip().lower()


def _posicao(original: str, normalizado: str, alias: str) -> int:
    a = _norm(alias)
    if not a:
        return -1
    if a == "pessoa":
        m = re.search(r"(?<!\w)(Pessoa|PESSOA)(?!\w)", original)
        if m:
            return m.start()
        # "pessoa, ..." no inicio da mensagem (vocativo em minusculas)
        m = re.match(r"\s*pessoa\b", original, re.I)
        return m.start() if m else -1
    m = re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", normalizado)
    return m.start() if m else -1


def interpretar(texto: str, ultimo_iniciador: str | None = None) -> dict | None:
    """Interpreta o pedido humano e monta um plano de falas."""
    original = texto or ""
    normalizado = _norm(original)
    if not normalizado:
        return None

    posicoes: dict[str, int] = {}
    for bot in BOTS:
        for alias in ALIASES.get(bot, [bot]):
            pos = _posicao(original, normalizado, alias)
            if pos >= 0:
                posicoes[bot] = min(pos, posicoes.get(bot, pos))
                break

    participantes = [bot for bot, _ in sorted(posicoes.items(), key=lambda kv: kv[1])]
    mencionado = bool(participantes)
    tem_verbo = bool(_VERBO.search(normalizado))
    contagem = _CONTAGEM.search(normalizado)

    if not participantes:
        participantes = list(BOTS)
    unicos: list[str] = []
    for bot in participantes:
        if bot not in unicos:
            unicos.append(bot)
    participantes = unicos
    if not participantes:
        return None

    if len(participantes) == 1:
        max_mensagens = 1
    elif contagem:
        n = int(contagem.group(1))
        unidade = contagem.group(2)
        if unidade.startswith("mensage"):
            max_mensagens = n
        else:
            max_mensagens = n * len(participantes)
    else:
        max_mensagens = RODADAS_PADRAO * len(participantes)

    max_mensagens = max(1, min(max_mensagens, MAX_MENSAGENS))

    if mencionado:
        inicio = participantes[0]
    elif ultimo_iniciador in participantes:
        idx = participantes.index(ultimo_iniciador)
        inicio = participantes[(idx + 1) % len(participantes)]
    else:
        inicio = next((b for b in ORDEM if b in participantes), participantes[0])

    base = participantes.index(inicio)
    ordem = [participantes[(base + i) % len(participantes)] for i in range(max_mensagens)]

    return {
        "participantes": participantes,
        "ordem": ordem,
        "max_mensagens": max_mensagens,
        "prompt": original,
        "mencionado": mencionado,
        "tem_verbo": tem_verbo,
        "contagem": contagem.group(0) if contagem else None,
    }


@dataclass
class Plano:
    id: str
    chat_id: int
    prompt: str
    participantes: list[str]
    ordem: list[str]
    max_mensagens: int
    indice: int = 0
    falas: list[dict] = field(default_factory=list)
    status: str = "ativo"
    destinatario: str | None = None
    expira: float = 0.0
    criado: float = field(default_factory=time.time)
    motivo: str = ""

    def rodada(self) -> int:
        return self.indice // max(1, len(self.participantes)) + 1

    def max_rodadas(self) -> int:
        return math.ceil(self.max_mensagens / max(1, len(self.participantes)))


class Hub:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.seq = int(time.time() * 1000)
        self.eventos: deque[dict] = deque(maxlen=5000)
        self.planos: dict[str, Plano] = {}
        self.ultimo_iniciador: dict[int, str] = {}
        self.vistos: set[tuple[int, int]] = set()
        self.fila_vistos: deque[tuple[int, int]] = deque(maxlen=5000)
        self._log_fh = None
        self._abrir_log()
        self._carregar_seq()
        threading.Thread(target=self._vigia, daemon=True).start()

    # ------------------------------------------------------------------
    # JSONL
    # ------------------------------------------------------------------
    def _abrir_log(self) -> None:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(STATE, "a", encoding="utf-8")
        except Exception as e:
            log.error("nao consegui abrir %s: %r", STATE, e)

    def _carregar_seq(self) -> None:
        try:
            if not STATE.exists():
                return
            linhas = STATE.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
            for ln in linhas:
                try:
                    ev = json.loads(ln)
                    self.seq = max(self.seq, int(ev.get("seq", 0)))
                except Exception:
                    continue
        except Exception as e:
            log.warning("falha ao ler %s: %r", STATE, e)

    def _proximo_seq(self) -> int:
        self.seq = max(self.seq + 1, int(time.time() * 1000))
        return self.seq

    def _registrar(self, evento: dict) -> dict:
        evento["seq"] = self._proximo_seq()
        evento["ts"] = time.time()
        self.eventos.append(evento)
        if self._log_fh:
            try:
                self._log_fh.write(json.dumps(evento, ensure_ascii=False) + "\n")
                self._log_fh.flush()
            except Exception as e:
                log.warning("falha ao gravar JSONL: %r", e)
        self.cond.notify_all()
        return evento

    # ------------------------------------------------------------------
    # Planos
    # ------------------------------------------------------------------
    def _cancelar_plano(self, plano: Plano, motivo: str) -> None:
        if plano.status != "ativo":
            return
        plano.status = "cancelado"
        plano.motivo = motivo
        plano.destinatario = None
        log.info("plano %s cancelado: %s", plano.id, motivo)
        self._registrar(
            {"tipo": "fim", "conversa_id": plano.id, "chat_id": plano.chat_id, "motivo": motivo}
        )

    def _encerrar(self, plano: Plano, motivo: str) -> None:
        if plano.status != "ativo":
            return
        plano.status = "concluido"
        plano.motivo = motivo
        plano.destinatario = None
        log.info(
            "plano %s encerrado: %s (%d/%d mensagens)",
            plano.id,
            motivo,
            plano.indice,
            plano.max_mensagens,
        )
        self._registrar(
            {
                "tipo": "fim",
                "conversa_id": plano.id,
                "chat_id": plano.chat_id,
                "motivo": motivo,
                "indice": plano.indice,
            }
        )

    def _despachar(self, plano: Plano) -> None:
        if plano.status != "ativo":
            return
        if plano.indice >= plano.max_mensagens:
            self._encerrar(plano, "concluido")
            return
        dest = plano.ordem[plano.indice]
        plano.destinatario = dest
        plano.expira = time.time() + TIMEOUT
        ultima = plano.falas[-1] if plano.falas else {}
        evento = {
            "tipo": "convite",
            "destinatario": dest,
            "conversa_id": plano.id,
            "chat_id": plano.chat_id,
            "indice": plano.indice,
            "rodada": plano.rodada(),
            "max_mensagens": plano.max_mensagens,
            "max_rodadas": plano.max_rodadas(),
            "prompt": plano.prompt,
            "transcript": plano.falas[-10:],
            "reply_to": ultima.get("message_id"),
            "participantes": plano.participantes,
            "permitir_passar": plano.max_mensagens <= len(plano.participantes),
        }
        self._registrar(evento)
        log.info(
            "plano %s: convite %d/%d -> %s (rodada %d)",
            plano.id,
            plano.indice + 1,
            plano.max_mensagens,
            dest,
            evento["rodada"],
        )

    # ------------------------------------------------------------------
    # Publicacao
    # ------------------------------------------------------------------
    def publicar(self, body: dict) -> tuple[int, dict]:
        tipo = (body.get("tipo") or "").strip()
        origem = (body.get("origem") or "").strip()
        if origem not in BOTS:
            return 400, {"erro": f"origem invalida: {origem!r}"}
        try:
            chat_id = int(body.get("chat_id"))
        except Exception:
            return 400, {"erro": "chat_id invalido"}

        with self.lock:
            if CHAT_IDS and chat_id not in CHAT_IDS:
                return 403, {"erro": "chat_id nao autorizado"}

            if tipo == "humano":
                return self._humano(body, origem, chat_id)
            if tipo == "fala":
                return self._fala(body, origem, chat_id)
            if tipo == "passou":
                return self._passou(body, origem, chat_id)
            return 400, {"erro": f"tipo desconhecido: {tipo!r}"}

    def _humano(self, body: dict, origem: str, chat_id: int) -> tuple[int, dict]:
        message_id = int(body.get("message_id") or 0)
        texto = (body.get("texto") or "").strip()
        if not texto:
            return 400, {"erro": "texto vazio"}
        if len(texto) > 8000:
            return 413, {"erro": "texto grande demais"}

        chave = (chat_id, message_id)
        if chave in self.vistos:
            return 200, {"ok": True, "duplicado": True}
        self.vistos.add(chave)
        self.fila_vistos.append(chave)
        if len(self.vistos) > 6000:
            antigo = self.fila_vistos.popleft()
            self.vistos.discard(antigo)

        for plano in list(self.planos.values()):
            if plano.chat_id == chat_id and plano.status == "ativo":
                self._cancelar_plano(plano, "nova mensagem humana")

        info = interpretar(texto, self.ultimo_iniciador.get(chat_id))
        if not info:
            return 400, {"erro": "nao foi possivel interpretar o pedido"}

        plano = Plano(
            id=f"{chat_id}:{message_id}",
            chat_id=chat_id,
            prompt=info["prompt"],
            participantes=info["participantes"],
            ordem=info["ordem"],
            max_mensagens=info["max_mensagens"],
        )
        self.planos[plano.id] = plano
        self.ultimo_iniciador[chat_id] = plano.ordem[0]
        log.info(
            "plano %s: %d participantes=%s max=%d inicio=%s",
            plano.id,
            len(plano.participantes),
            ",".join(plano.participantes),
            plano.max_mensagens,
            plano.ordem[0],
        )
        self._despachar(plano)
        return 200, {
            "ok": True,
            "plano": plano.id,
            "participantes": plano.participantes,
            "max_mensagens": plano.max_mensagens,
        }

    def _fala(self, body: dict, origem: str, chat_id: int) -> tuple[int, dict]:
        conversa_id = (body.get("conversa_id") or "").strip()
        plano = self.planos.get(conversa_id)
        if not plano or plano.chat_id != chat_id:
            return 404, {"erro": "plano nao encontrado"}
        if plano.status != "ativo":
            return 409, {"erro": f"plano {plano.status}"}
        if plano.destinatario != origem:
            return 409, {"erro": f"nao e a vez de {origem}"}
        try:
            indice = int(body.get("indice"))
        except Exception:
            return 400, {"erro": "indice invalido"}
        if indice != plano.indice:
            return 409, {"erro": "indice fora de ordem"}

        texto = (body.get("texto") or "").strip()
        if not texto:
            return 400, {"erro": "texto vazio"}
        if len(texto) > 8000:
            return 413, {"erro": "texto grande demais"}

        plano.falas.append(
            {
                "autor": origem,
                "autor_nome": (body.get("autor_nome") or origem).strip(),
                "texto": texto,
                "message_id": body.get("message_id"),
                "reply_to": body.get("reply_to"),
                "rodada": plano.rodada(),
            }
        )
        plano.indice += 1
        log.info(
            "plano %s: fala %d/%d de %s",
            plano.id,
            plano.indice,
            plano.max_mensagens,
            origem,
        )
        self._despachar(plano)
        return 200, {"ok": True, "indice": plano.indice}

    def _passou(self, body: dict, origem: str, chat_id: int) -> tuple[int, dict]:
        conversa_id = (body.get("conversa_id") or "").strip()
        plano = self.planos.get(conversa_id)
        if not plano or plano.chat_id != chat_id:
            return 404, {"erro": "plano nao encontrado"}
        if plano.status != "ativo":
            return 409, {"erro": f"plano {plano.status}"}
        if plano.destinatario != origem:
            return 409, {"erro": f"nao e a vez de {origem}"}
        motivo = (body.get("motivo") or "passou").strip()
        self._encerrar(plano, f"passou: {motivo}")
        return 200, {"ok": True}

    # ------------------------------------------------------------------
    # Long-poll
    # ------------------------------------------------------------------
    def _convite_ativo(self, evento: dict) -> bool:
        if evento.get("tipo") != "convite":
            return False
        plano = self.planos.get(evento.get("conversa_id"))
        return bool(
            plano
            and plano.status == "ativo"
            and plano.destinatario == evento.get("destinatario")
            and plano.indice == evento.get("indice")
        )

    def esperar(self, destinatario: str, desde: int, timeout: float) -> dict:
        limite = time.monotonic() + max(0.0, min(timeout, 25.0))
        with self.lock:
            while True:
                disponiveis = [
                    e
                    for e in self.eventos
                    if e.get("seq", 0) > desde
                    and e.get("destinatario") == destinatario
                    and self._convite_ativo(e)
                ]
                if disponiveis:
                    return {
                        "seq": max(e["seq"] for e in disponiveis),
                        "eventos": disponiveis[:5],
                    }
                restante = limite - time.monotonic()
                if restante <= 0:
                    return {"seq": self.seq, "eventos": []}
                self.cond.wait(min(restante, 1.0))

    def saude(self) -> dict:
        with self.lock:
            ativos = sum(1 for p in self.planos.values() if p.status == "ativo")
            return {
                "ok": True,
                "seq": self.seq,
                "planos_ativos": ativos,
                "bots": BOTS,
                "chat_ids": sorted(CHAT_IDS),
            }

    def _vigia(self) -> None:
        while True:
            time.sleep(1.0)
            agora = time.time()
            with self.lock:
                for plano in self.planos.values():
                    if plano.status == "ativo" and plano.expira and agora > plano.expira:
                        self._encerrar(plano, "timeout")
                self.cond.notify_all()


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(corpo)))
    handler.end_headers()
    handler.wfile.write(corpo)


def _autorizado(handler: BaseHTTPRequestHandler) -> bool:
    if not TOKEN:
        return False
    recebido = handler.headers.get("Authorization", "")
    return hmac.compare_digest(recebido, f"Bearer {TOKEN}")


class Handler(BaseHTTPRequestHandler):
    hub = None  # type: Hub

    def log_message(self, fmt, *args):  # noqa: D401
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _corpo(self) -> dict:
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > 16384:
            raise ValueError("corpo grande demais")
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        return json.loads(bruto.decode("utf-8", errors="replace") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/saude":
            _json(self, 200, self.hub.saude())
            return
        if not _autorizado(self):
            _json(self, 401, {"erro": "nao autorizado"})
            return
        if parsed.path == "/seq":
            with self.hub.lock:
                _json(self, 200, {"seq": self.hub.seq})
            return
        if parsed.path == "/planos":
            with self.hub.lock:
                planos = [
                    {
                        "id": p.id,
                        "chat_id": p.chat_id,
                        "status": p.status,
                        "indice": p.indice,
                        "max": p.max_mensagens,
                        "destinatario": p.destinatario,
                        "participantes": p.participantes,
                    }
                    for p in self.hub.planos.values()
                    if p.status == "ativo"
                ]
            _json(self, 200, {"planos": planos})
            return
        if parsed.path == "/eventos":
            q = parse_qs(parsed.query)
            destino = (q.get("destinatario") or [""])[0]
            if destino not in BOTS:
                _json(self, 400, {"erro": "destinatario invalido"})
                return
            try:
                desde = int((q.get("desde") or ["0"])[0])
                timeout = float((q.get("timeout") or ["25"])[0])
            except Exception:
                _json(self, 400, {"erro": "desde/timeout invalidos"})
                return
            _json(self, 200, self.hub.esperar(destino, desde, timeout))
            return
        _json(self, 404, {"erro": "rota nao encontrada"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/eventos" or not _autorizado(self):
            _json(self, 401 if not _autorizado(self) else 404, {"erro": "nao autorizado"})
            return
        try:
            body = self._corpo()
            code, payload = self.hub.publicar(body)
        except Exception as e:
            log.warning("POST /eventos invalido: %r", e)
            code, payload = 400, {"erro": str(e)}
        _json(self, code, payload)


def main() -> None:
    if not TOKEN:
        log.error("BARRAMENTO_TOKEN nao definido; barramento nao inicia")
        sys.exit(1)
    if not BOTS:
        log.error("BARRAMENTO_BOTS vazio; barramento nao inicia")
        sys.exit(1)
    Handler.hub = Hub()
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Handler)
    servidor.daemon_threads = True
    log.info(
        "barramento em :%d bots=%s chat_ids=%s jsonl=%s",
        PORTA,
        ",".join(BOTS),
        ",".join(str(x) for x in sorted(CHAT_IDS)) or "(todos)",
        STATE,
    )
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()