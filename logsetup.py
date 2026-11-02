#!/usr/bin/env python3
"""Configuracao de logging: tudo vai para stdout/stderr (12-factor).

No Docker aparece em `docker logs`; no host o `run_bot.sh` redireciona para
`STATE_DIR/bot.log`. Nao ha gravação de arquivo dentro da aplicacao.
"""
import logging
import sys

import config

_LOGGER_ROOT = config.AGENT

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger(_LOGGER_ROOT)
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")
