"""Dados do condomínio em SQLite: schema, restauração e todas as leituras/gravações.

Regras críticas que vivem aqui (e não no prompt):
- Exclusividade de reserva no instante da gravação: índice único parcial
  `ux_reserva_ativa (area, data) WHERE status = 'ativa'` (Garantia 5).
- Código de reserva gerado pelo sistema a partir de um id AUTOINCREMENT (nunca
  reutilizado) + UNIQUE em todas as linhas, inclusive canceladas (regra 5).
- Toda consulta/alteração de reservas e visitantes recebe o apartamento já resolvido
  da sessão e filtra por ele (Garantia 2).
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Iterator

from . import config

CODIGO_PREFIXO = "RSV-A"   # códigos iniciais são RSV-<4 dígitos>; os novos nunca colidem com eles

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reservas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo      TEXT NOT NULL UNIQUE,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL REFERENCES areas(id),
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa ON reservas(area, data) WHERE status = 'ativa';
CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    criada_em   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS confirmacoes (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessoes(session_id),
    function_call_id TEXT NOT NULL,
    acao             TEXT NOT NULL,
    detalhes         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente', 'respondida')),
    confirmado       INTEGER,
    criada_em        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class DataIndisponivel(Exception):
    """A área já tem reserva ativa nessa data (violação do índice único parcial)."""


def connect() -> sqlite3.Connection:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    """Transação explícita com lock de escrita desde o início (BEGIN IMMEDIATE)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_schema() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _ler_json(nome: str) -> list[dict]:
    return json.loads((config.DADOS_DIR / nome).read_text(encoding="utf-8"))


def restaurar() -> None:
    """Volta o condomínio ao estado de dados/*.json e limpa sessões/confirmações."""
    init_schema()
    with transacao() as conn:
        conn.execute("DELETE FROM confirmacoes")
        conn.execute("DELETE FROM sessoes")
        conn.execute("DELETE FROM visitantes")
        conn.execute("DELETE FROM reservas")
        conn.execute("DELETE FROM areas")
        conn.execute("DELETE FROM apartamentos")
        # sqlite_sequence NÃO é zerado: ids (e portanto códigos) nunca se repetem, nem após restaurar.
        conn.executemany("INSERT INTO apartamentos (numero, morador) VALUES (:numero, :morador)",
                         _ler_json("apartamentos.json"))
        conn.executemany("INSERT INTO areas (id, nome, taxa) VALUES (:id, :nome, :taxa)",
                         _ler_json("areas.json"))
        conn.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (:codigo, :apartamento, :area, :data)",
            _ler_json("reservas.json"))
        conn.executemany("INSERT INTO visitantes (apartamento, nome, data) VALUES (:apartamento, :nome, :data)",
                         _ler_json("visitantes.json"))


def garantir_dados() -> None:
    """Cria o schema e, se o banco estiver vazio (primeira subida), carrega os dados iniciais."""
    init_schema()
    conn = connect()
    try:
        vazio = conn.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0
    finally:
        conn.close()
    if vazio:
        restaurar()


# ---------------------------------------------------------------- validação de entrada

def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().replace("-", " ").replace("_", " ").split())


def resolver_area(texto: str) -> dict | None:
    """Converte o que o modelo/morador escreveu ("salão de festas", "salao-de-festas") no id da área."""
    alvo = _normalizar(texto or "")
    if not alvo:
        return None
    conn = connect()
    try:
        areas = [dict(r) for r in conn.execute("SELECT id, nome, taxa FROM areas")]
    finally:
        conn.close()
    for area in areas:
        if alvo in (_normalizar(area["id"]), _normalizar(area["nome"])):
            return area
    candidatas = [a for a in areas
                  if alvo in _normalizar(a["nome"]) or _normalizar(a["id"]) in alvo or _normalizar(a["nome"]) in alvo]
    return candidatas[0] if len(candidatas) == 1 else None


def nomes_areas() -> list[str]:
    conn = connect()
    try:
        return [f"{r['nome']} ({r['id']})" for r in conn.execute("SELECT id, nome FROM areas ORDER BY nome")]
    finally:
        conn.close()


def validar_data(texto: str) -> str | None:
    try:
        return date.fromisoformat((texto or "").strip()).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- sessões e confirmações

def registrar_sessao(session_id: str, apartamento: str) -> None:
    with transacao() as conn:
        conn.execute("INSERT INTO sessoes (session_id, apartamento) VALUES (?, ?)", (session_id, apartamento))


def apartamento_da_sessao(session_id: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)).fetchone()
        return row["apartamento"] if row else None
    finally:
        conn.close()


def registrar_confirmacao(session_id: str, function_call_id: str, acao: str, detalhes: dict) -> None:
    with transacao() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO confirmacoes (id, session_id, function_call_id, acao, detalhes)"
            " VALUES (?, ?, ?, ?, ?)",
            (function_call_id, session_id, function_call_id, acao, json.dumps(detalhes, ensure_ascii=False)))


def confirmacoes_pendentes(session_id: str) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, acao, detalhes FROM confirmacoes WHERE session_id = ? AND status = 'pendente'"
            " ORDER BY criada_em, rowid", (session_id,)).fetchall()
        return [{"id": r["id"], "acao": r["acao"], "detalhes": json.loads(r["detalhes"])} for r in rows]
    finally:
        conn.close()


def responder_confirmacao(session_id: str, confirmacao_id: str, confirmado: bool) -> str | None:
    """Transição atômica pendente → respondida. Devolve o function_call_id, ou None (→ 409).

    O UPDATE condicionado ao status é a única porta: uma confirmação inexistente, de outra
    sessão ou já respondida afeta 0 linhas e nada é executado (Garantia 1).
    """
    with transacao() as conn:
        cur = conn.execute(
            "UPDATE confirmacoes SET status = 'respondida', confirmado = ?"
            " WHERE id = ? AND session_id = ? AND status = 'pendente'",
            (1 if confirmado else 0, confirmacao_id, session_id))
        if cur.rowcount != 1:
            return None
        return conn.execute("SELECT function_call_id FROM confirmacoes WHERE id = ?",
                            (confirmacao_id,)).fetchone()["function_call_id"]


# ---------------------------------------------------------------- reservas

def reservas_ativas(apartamento: str) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND status = 'ativa' ORDER BY data, id",
            (apartamento,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def data_livre(area_id: str, data: str) -> bool:
    """Só diz se está livre; nunca revela de quem é a reserva."""
    conn = connect()
    try:
        return conn.execute("SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
                            (area_id, data)).fetchone() is None
    finally:
        conn.close()


def criar_reserva(apartamento: str, area_id: str, data: str) -> str:
    """Grava a reserva; a exclusividade é decidida pelo índice único parcial no INSERT."""
    try:
        with transacao() as conn:
            cur = conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, status) VALUES (?, ?, ?, ?, 'ativa')",
                (f"tmp-{uuid.uuid4()}", apartamento, area_id, data))
            codigo = f"{CODIGO_PREFIXO}{cur.lastrowid:04d}"
            conn.execute("UPDATE reservas SET codigo = ? WHERE id = ?", (codigo, cur.lastrowid))
            return codigo
    except sqlite3.IntegrityError as exc:
        if "reservas.area" in str(exc) or "ux_reserva_ativa" in str(exc):
            raise DataIndisponivel() from exc
        raise


def cancelar_reserva(apartamento: str, codigo: str | None, area_id: str | None, data: str | None) -> str | None:
    """Cancela (status, não apaga) uma reserva ATIVA do próprio apartamento. Devolve o código ou None."""
    with transacao() as conn:
        if codigo:
            row = conn.execute(
                "SELECT id, codigo FROM reservas WHERE codigo = ? AND apartamento = ? AND status = 'ativa'",
                (codigo.strip().upper(), apartamento)).fetchone()
        else:
            row = conn.execute(
                "SELECT id, codigo FROM reservas WHERE area = ? AND data = ? AND apartamento = ? AND status = 'ativa'",
                (area_id, data, apartamento)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE reservas SET status = 'cancelada' WHERE id = ?", (row["id"],))
        return row["codigo"]


# ---------------------------------------------------------------- visitantes

def visitantes(apartamento: str) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
                            (apartamento,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with transacao() as conn:
        conn.execute("INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)", (apartamento, nome, data))
