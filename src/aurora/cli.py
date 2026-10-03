"""Comandos do projeto: `uv run aurora-restaurar` e `uv run aurora-api`."""
from __future__ import annotations

import uvicorn

from . import config, db


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de dados/*.json e apaga sessões e confirmações."""
    db.restaurar()
    for arquivo in (config.SESSIONS_DB_PATH, config.SESSIONS_DB_PATH.with_name(config.SESSIONS_DB_PATH.name + "-wal"),
                    config.SESSIONS_DB_PATH.with_name(config.SESSIONS_DB_PATH.name + "-shm")):
        arquivo.unlink(missing_ok=True)
    print(f"Dados restaurados em {config.DB_PATH} (reservas, visitantes, sessões e confirmações).")


def api() -> None:
    """Sobe a API em http://localhost:8000."""
    uvicorn.run("aurora.api:app", host=config.API_HOST, port=config.API_PORT)
