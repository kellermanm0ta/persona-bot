#!/usr/bin/env python3
"""Configuracao do bot, lida de variaveis de ambiente (12-factor).

O mesmo codigo serve para qualquer persona/contexto: cada instancia/container
define `AGENT`, `RAG_ENABLED`, `CONTEXT_DIR`, etc. Segredos (token do Telegram,
chave da OpenRouter) chegam por `env_file`/`.secrets`. Ver AGENTS.md / README.md.
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


# Chave/modelo da OpenRouter e, opcionalmente, EMBED_MODEL.
_load_env_file(BASE / ".secrets" / "openrouter.env")


def _flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default).strip() or default)
    return p if p.is_absolute() else (BASE / p)


AGENT = os.environ.get("AGENT", "bukowski").strip() or "bukowski"
RAG_ENABLED = _flag("RAG_ENABLED", False)
RETRIEVAL = os.environ.get("RETRIEVAL", "hybrid").strip().lower() or "hybrid"
EMBED_MODEL = os.environ.get(
    "EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
).strip()
CONTEXT_DIR = _path("CONTEXT_DIR", f"contexto/{AGENT}")
STATE_DIR = _path("STATE_DIR", f"data/{AGENT}")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "12") or "12")
INDEX_ON_START = _flag("INDEX_ON_START", True)
