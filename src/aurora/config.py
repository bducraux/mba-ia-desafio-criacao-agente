"""Configuração lida do ambiente (.env), com defaults para rodar localmente."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

DADOS_DIR = ROOT_DIR / "dados"
VAR_DIR = Path(os.environ.get("AURORA_VAR_DIR") or ROOT_DIR / "var")

DB_PATH = VAR_DIR / "aurora.db"
SESSIONS_DB_PATH = VAR_DIR / "sessions.db"
SESSIONS_DB_URL = f"sqlite+aiosqlite:///{SESSIONS_DB_PATH}"

APP_NAME = "residencial_aurora"
# `or` (e não default do get): variável presente porém vazia no .env também cai no padrão.
MODEL = os.environ.get("AURORA_MODEL") or "gemini-3.8-flash"

API_HOST = os.environ.get("AURORA_HOST") or "0.0.0.0"
API_PORT = int(os.environ.get("AURORA_PORT") or "8000")
