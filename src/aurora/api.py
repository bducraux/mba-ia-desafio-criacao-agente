"""API HTTP do assistente (contrato do desafio) sobre o Runner do ADK."""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel

from . import config, db, tools
from .agents import build_root_agent

CONFIRMACAO_FC = "adk_request_confirmation"


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


class Assistente:
    """Runner do ADK + sessões persistidas em SQLite (Garantia 3)."""

    def __init__(self) -> None:
        config.VAR_DIR.mkdir(parents=True, exist_ok=True)
        # Sessões e eventos do ADK em arquivo: sobrevivem ao reinício da API.
        self.sessoes = DatabaseSessionService(db_url=config.SESSIONS_DB_URL, connect_args={"timeout": 30})
        self.runner = Runner(app=App(name=config.APP_NAME, root_agent=build_root_agent()),
                             session_service=self.sessoes)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def user_id(apartamento: str) -> str:
        return f"apto-{apartamento}"

    async def criar_sessao(self, apartamento: str) -> str:
        session_id = uuid.uuid4().hex
        # O apartamento é gravado UMA vez: no state da sessão e na tabela `sessoes` (Garantia 2).
        await self.sessoes.create_session(app_name=config.APP_NAME, user_id=self.user_id(apartamento),
                                          session_id=session_id, state={"apartamento": apartamento})
        db.registrar_sessao(session_id, apartamento)
        return session_id

    async def sessao(self, session_id: str):
        apartamento = db.apartamento_da_sessao(session_id)
        if apartamento is None:
            raise HTTPException(status_code=404, detail="Sessão não encontrada.")
        session = await self.sessoes.get_session(app_name=config.APP_NAME, user_id=self.user_id(apartamento),
                                                 session_id=session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Sessão não encontrada.")
        return apartamento, session

    async def executar(self, session_id: str, apartamento: str, mensagem: types.Content) -> dict:
        """Roda o Runner, registra pedidos de confirmação e monta a resposta do contrato."""
        resposta = ""
        async with self._locks[session_id]:
            async for evento in self.runner.run_async(user_id=self.user_id(apartamento), session_id=session_id,
                                                      new_message=mensagem):
                self._registrar_confirmacoes(session_id, evento)
                texto = self._texto(evento)
                if texto:
                    resposta = texto
        return {"resposta": resposta, "confirmacoes_pendentes": db.confirmacoes_pendentes(session_id)}

    @staticmethod
    def _texto(evento: Event) -> str:
        if evento.author == "user" or not evento.content or not evento.content.parts:
            return ""
        return "".join(p.text for p in evento.content.parts if p.text and not p.thought).strip()

    @staticmethod
    def _registrar_confirmacoes(session_id: str, evento: Event) -> None:
        for fc in evento.get_function_calls():
            if fc.name != CONFIRMACAO_FC or not fc.id:
                continue
            original = (fc.args or {}).get("originalFunctionCall") or {}
            nome_tool = original.get("name", "")
            db.registrar_confirmacao(
                session_id=session_id,
                function_call_id=fc.id,
                acao=tools.ACOES_CONFIRMAVEIS.get(nome_tool, nome_tool),
                detalhes=tools.detalhes_da_acao(nome_tool, original.get("args") or {}),
            )


assistente: Assistente | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global assistente
    db.garantir_dados()
    assistente = Assistente()
    yield


app = FastAPI(title="Assistente do Residencial Aurora", lifespan=lifespan)


@app.post("/sessoes", status_code=201)
async def criar_sessao(body: NovaSessao) -> dict:
    apartamento = body.apartamento.strip()
    if not apartamento:
        raise HTTPException(status_code=422, detail="Informe o apartamento.")
    return {"session_id": await assistente.criar_sessao(apartamento)}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, body: NovaMensagem) -> dict:
    apartamento, _ = await assistente.sessao(session_id)
    mensagem = types.Content(role="user", parts=[types.Part(text=body.texto)])
    return await assistente.executar(session_id, apartamento, mensagem)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, body: RespostaConfirmacao):
    apartamento, _ = await assistente.sessao(session_id)
    # Transição atômica pendente → respondida; qualquer outro id (inexistente, de outra sessão
    # ou já respondido) cai aqui com 409 e nada é executado (Garantia 1).
    function_call_id = db.responder_confirmacao(session_id, body.id, body.confirmado)
    if function_call_id is None:
        return JSONResponse(status_code=409,
                            content={"detail": "Não existe confirmação pendente com esse id nesta sessão."})
    # A resposta vai para o Runner como FunctionResponse de adk_request_confirmation; o Runner a
    # entrega ao especialista que pediu a confirmação, que então executa (ou não) a tool.
    mensagem = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=function_call_id, name=CONFIRMACAO_FC, response={"confirmed": body.confirmado}))])
    return await assistente.executar(session_id, apartamento, mensagem)


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list:
    _, session = await assistente.sessao(session_id)
    return [evento.model_dump(mode="json", exclude_none=True) for evento in session.events]


@app.get("/apartamentos/{numero}/reservas")
def reservas_do_apartamento(numero: str) -> list:
    return db.reservas_ativas(numero)


@app.get("/apartamentos/{numero}/visitantes")
def visitantes_do_apartamento(numero: str) -> list:
    return db.visitantes(numero)
