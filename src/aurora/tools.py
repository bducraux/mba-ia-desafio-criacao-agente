"""Tools dos especialistas: o único caminho do modelo até os dados do condomínio.

Regras aplicadas em código, independentemente do que o modelo decidir:
- Nenhuma tool recebe apartamento: ele vem da sessão (tabela `sessoes`, gravada uma vez na
  criação, conferida contra o state da sessão) — Garantia 2.
- Reserva de área com taxa e autorização de visitante exigem confirmação do sistema
  (`require_confirmation` do ADK) e, por defesa em profundidade, a própria tool recusa gravar
  sem `tool_confirmation.confirmed` — Garantia 1.
- Respostas nunca trazem dados de outro apartamento: disponibilidade é só livre/ocupada e
  "não encontrada" não diz se a reserva existe para outro morador.
"""
from __future__ import annotations

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from . import db, regulamento


class SessaoInvalida(Exception):
    pass


def _apartamento(tool_context: ToolContext) -> str:
    """Apartamento autenticado da sessão; nunca um valor escolhido pelo modelo."""
    session = tool_context.session
    apartamento = db.apartamento_da_sessao(session.id)
    if apartamento is None or session.state.get("apartamento") != apartamento:
        raise SessaoInvalida("Sessão sem apartamento válido.")
    return apartamento


def _area_ou_erro(area: str) -> tuple[dict | None, dict | None]:
    encontrada = db.resolver_area(area)
    if encontrada is None:
        return None, {"ok": False, "erro": "Área não reconhecida.", "areas_disponiveis": db.nomes_areas()}
    return encontrada, None


def _data_ou_erro(data: str) -> tuple[str | None, dict | None]:
    valida = db.validar_data(data)
    if valida is None:
        return None, {"ok": False, "erro": "Data inválida. Use o formato AAAA-MM-DD."}
    return valida, None


# ------------------------------------------------------------------- reservas

def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    return {"ok": True, "reservas": db.reservas_ativas(_apartamento(tool_context))}


def verificar_disponibilidade(area: str, data: str, tool_context: ToolContext) -> dict:
    """Informa se uma área comum está livre ou ocupada numa data (AAAA-MM-DD).

    Args:
        area: nome ou id da área (salão de festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.
    """
    _apartamento(tool_context)
    area_info, erro = _area_ou_erro(area)
    if erro:
        return erro
    data_ok, erro = _data_ou_erro(data)
    if erro:
        return erro
    situacao = "livre" if db.data_livre(area_info["id"], data_ok) else "ocupada"
    return {"ok": True, "area": area_info["id"], "data": data_ok, "situacao": situacao}


def _reserva_precisa_confirmacao(area: str = "", data: str = "", **_) -> bool:
    """Reservar área com taxa > 0 gera cobrança → exige confirmação (regra 2)."""
    area_info = db.resolver_area(area)
    return bool(area_info and area_info["taxa"] > 0)


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador. Áreas com taxa só são
    reservadas depois que o morador aprova a cobrança pelo sistema de confirmações.

    Args:
        area: nome ou id da área (salão de festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    area_info, erro = _area_ou_erro(area)
    if erro:
        return erro
    data_ok, erro = _data_ou_erro(data)
    if erro:
        return erro
    if area_info["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None or not confirmacao.confirmed:
            return {"ok": False, "reservado": False,
                    "erro": "Reserva NÃO realizada: a cobrança não foi aprovada pelo sistema de confirmações. Nada foi gravado."}
    try:
        codigo = db.criar_reserva(apartamento, area_info["id"], data_ok)
    except db.DataIndisponivel:
        return {"ok": False, "reservado": False,
                "erro": f"Reserva NÃO realizada: {area_info['nome']} já está ocupado(a) em {data_ok}. Escolha outra data."}
    return {"ok": True, "reservado": True, "codigo": codigo, "area": area_info["id"], "data": data_ok,
            "taxa": area_info["taxa"]}


def cancelar_minha_reserva(codigo: str = "", area: str = "", data: str = "", tool_context: ToolContext = None) -> dict:
    """Cancela uma reserva ativa do PRÓPRIO apartamento, pelo código OU pela área + data.

    Args:
        codigo: código da reserva (opcional se área e data forem informadas).
        area: nome ou id da área (opcional se o código for informado).
        data: data AAAA-MM-DD (opcional se o código for informado).
    """
    apartamento = _apartamento(tool_context)
    area_id = data_ok = None
    if not codigo:
        area_info, erro = _area_ou_erro(area)
        if erro:
            return erro
        data_ok, erro = _data_ou_erro(data)
        if erro:
            return erro
        area_id = area_info["id"]
    cancelado = db.cancelar_reserva(apartamento, codigo or None, area_id, data_ok)
    if cancelado is None:
        return {"ok": False, "cancelado": False,
                "erro": "Nenhuma reserva ativa do seu apartamento corresponde a esses dados. Nada foi alterado."}
    return {"ok": True, "cancelado": True, "codigo": cancelado}


# ------------------------------------------------------------------- visitantes

def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados para o apartamento do morador desta conversa."""
    return {"ok": True, "visitantes": db.visitantes(_apartamento(tool_context))}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante numa data. Sempre exige aprovação do morador pelo
    sistema de confirmações — mensagens no chat não contam como confirmação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    nome = " ".join((nome or "").split())
    if not nome:
        return {"ok": False, "erro": "Informe o nome do visitante."}
    data_ok, erro = _data_ou_erro(data)
    if erro:
        return erro
    confirmacao = tool_context.tool_confirmation
    if confirmacao is None or not confirmacao.confirmed:
        return {"ok": False, "autorizado": False,
                "erro": "Entrada NÃO liberada: a autorização não foi aprovada pelo sistema de confirmações. Nada foi gravado."}
    db.autorizar_visitante(apartamento, nome, data_ok)
    return {"ok": True, "autorizado": True, "nome": nome, "data": data_ok}


# ------------------------------------------------------------------- regulamento

def consultar_regulamento(assunto: str) -> dict:
    """Busca no regulamento interno o capítulo que trata do assunto e devolve só esse capítulo.

    Args:
        assunto: tema da dúvida, com palavras-chave (ex.: "piscina horário domingo").
    """
    capitulo = regulamento.buscar(assunto)
    if capitulo is None:
        return {"ok": False, "erro": "Nenhum capítulo corresponde ao assunto.", "indice": regulamento.indice()}
    return {"ok": True, "capitulo": f"Capítulo {capitulo.numero}: {capitulo.titulo}", "texto": capitulo.texto}


# ------------------------------------------------------------------- registro das tools

TOOLS_RESERVAS = [
    FunctionTool(listar_minhas_reservas),
    FunctionTool(verificar_disponibilidade),
    FunctionTool(reservar_area, require_confirmation=_reserva_precisa_confirmacao),
    FunctionTool(cancelar_minha_reserva),
]
TOOLS_VISITANTES = [
    FunctionTool(listar_meus_visitantes),
    FunctionTool(autorizar_visitante, require_confirmation=True),
]
TOOLS_REGULAMENTO = [FunctionTool(consultar_regulamento)]

# Ações que geram pendência, com os detalhes mostrados em `confirmacoes_pendentes`.
ACOES_CONFIRMAVEIS = {"reservar_area": "reservar_area_com_taxa", "autorizar_visitante": "autorizar_visitante"}


def detalhes_da_acao(tool_name: str, args: dict) -> dict:
    if tool_name == "reservar_area":
        area_info = db.resolver_area(args.get("area", ""))
        return {"area": area_info["id"] if area_info else args.get("area"),
                "data": db.validar_data(args.get("data", "")) or args.get("data"),
                "taxa": area_info["taxa"] if area_info else None}
    if tool_name == "autorizar_visitante":
        return {"nome": " ".join(str(args.get("nome", "")).split()),
                "data": db.validar_data(args.get("data", "")) or args.get("data")}
    return dict(args)
