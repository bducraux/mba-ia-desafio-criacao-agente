"""Consulta ao regulamento por capítulo (Garantia 4).

O regulamento nunca entra inteiro em instruções nem em eventos: a tool devolve apenas o
capítulo escolhido por casamento determinístico de palavras-chave (título do capítulo +
sinônimos), e o especialista conhece só o índice de títulos.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from . import config

# Sinônimos por número do capítulo (romano), além das palavras do próprio título.
SINONIMOS: dict[str, tuple[str, ...]] = {
    "I": ("geral", "gerais", "aplicacao", "administracao", "sindica", "sindico", "condominio", "areas comuns"),
    "II": ("direito", "direitos", "dever", "deveres", "morador", "inquilino", "locatario"),
    "III": ("silencio", "barulho", "ruido", "som", "musica", "convivencia", "vizinho", "vizinhos"),
    "IV": ("piscina", "nadar", "natacao", "deck", "banho"),
    "V": ("academia", "ginastica", "musculacao", "brinquedoteca", "playground", "parquinho", "criancas"),
    "VI": ("salao", "festas", "festa", "churrasqueira", "churrasco", "quadra", "reserva", "reservas", "taxa"),
    "VII": ("portaria", "porteiro", "seguranca", "visitante", "visitantes", "entrada", "acesso", "encomenda", "entregador"),
    "VIII": ("animal", "animais", "cachorro", "cao", "gato", "pet", "pets", "estimacao"),
    "IX": ("mudanca", "mudancas", "mudar", "caminhao"),
    "X": ("obra", "obras", "reforma", "reformas", "pedreiro"),
    "XI": ("garagem", "vaga", "vagas", "carro", "veiculo", "veiculos", "moto", "estacionamento", "estacionar"),
    "XII": ("lixo", "reciclagem", "coleta", "reciclavel", "residuos"),
    "XIII": ("infracao", "infracoes", "multa", "multas", "penalidade", "penalidades", "advertencia", "punicao"),
    "XIV": ("final", "finais", "vigencia", "omissos", "alteracao do regulamento"),
}

STOPWORDS = {"e", "de", "da", "do", "das", "dos", "a", "o", "as", "os", "capitulo"}


@dataclass(frozen=True)
class Capitulo:
    numero: str
    titulo: str
    texto: str
    palavras: frozenset[str]


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", sem_acento.lower())


@lru_cache(maxsize=1)
def capitulos() -> tuple[Capitulo, ...]:
    conteudo = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    partes = re.split(r"(?m)^## ", conteudo)[1:]
    resultado = []
    for parte in partes:
        cabecalho = parte.splitlines()[0].strip()          # "Capítulo IV: Piscina"
        m = re.match(r"Cap[ií]tulo\s+([IVXLC]+)\s*:\s*(.+)", cabecalho)
        numero, titulo = (m.group(1), m.group(2).strip()) if m else (cabecalho, cabecalho)
        palavras = {p for p in _normalizar(titulo).split() if p not in STOPWORDS}
        palavras.update(SINONIMOS.get(numero, ()))
        resultado.append(Capitulo(numero, titulo, "## " + parte.strip(), frozenset(palavras)))
    return tuple(resultado)


def indice() -> str:
    """Só os títulos — é isso que o especialista recebe nas instruções."""
    return "\n".join(f"- Capítulo {c.numero}: {c.titulo}" for c in capitulos())


def buscar(assunto: str) -> Capitulo | None:
    """Escolhe UM capítulo pela maior quantidade de palavras-chave presentes no assunto."""
    alvo = _normalizar(assunto or "")
    tokens = set(alvo.split())
    melhor, melhor_pontos = None, 0
    for cap in capitulos():
        pontos = sum(1 for p in cap.palavras if (" " in p and p in alvo) or p in tokens)
        if pontos > melhor_pontos:
            melhor, melhor_pontos = cap, pontos
    return melhor
