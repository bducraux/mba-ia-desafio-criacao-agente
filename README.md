# Assistente virtual do Residencial Aurora

API em Python (FastAPI) com um assistente construído em **Google ADK 2.11.0** e modelos Gemini, que reserva áreas comuns, cancela reservas, autoriza visitantes e tira dúvidas do regulamento — com as regras críticas do condomínio garantidas **no código**, não no prompt.

> O modelo decide o caminho; o código decide o que é permitido.

```
src/aurora/
├── api.py           # rotas HTTP (contrato do desafio) + Runner do ADK + registro de confirmações
├── agents.py        # concierge (principal) + 3 especialistas
├── tools.py         # tools dos especialistas (único caminho do modelo até os dados)
├── db.py            # SQLite: schema, restauração, leituras/gravações com as regras de negócio
├── regulamento.py   # divisão do regulamento por capítulos e busca determinística
├── config.py        # caminhos, modelo e porta (via .env)
└── cli.py           # comandos aurora-api e aurora-restaurar
```

## Arquitetura

### Agentes

| Agente | Responsabilidade | Tools | Como é acionado |
| --- | --- | --- | --- |
| **concierge** (principal) | Conversa com o morador, identifica a intenção e delega. Não acessa dados, não executa ações e **não recebe o regulamento nas instruções**. | nenhuma (só `transfer_to_agent`) | Raiz do `App`; recebe toda mensagem nova |
| **especialista_reservas** | Lista, verifica disponibilidade, reserva e cancela reservas do apartamento da sessão | `listar_minhas_reservas`, `verificar_disponibilidade`, `reservar_area` (confirmação se taxa > 0), `cancelar_minha_reserva` | `sub_agent` do concierge, por transferência |
| **especialista_visitantes** | Lista e autoriza visitantes do apartamento da sessão | `listar_meus_visitantes`, `autorizar_visitante` (sempre com confirmação) | `sub_agent`, por transferência |
| **especialista_regulamento** | Responde dúvidas do regulamento a partir do capítulo pertinente | `consultar_regulamento` | `sub_agent`, por transferência; nas instruções recebe só o **índice de títulos** |

Todos usam `gemini-3.8-flash` (configurável por `AURORA_MODEL`) com retry/backoff para 429/5xx. Os especialistas devolvem a conversa ao concierge quando o assunto muda.

### Por que especialistas por transferência (`sub_agents`) e não `AgentTool`

A confirmação de tools do ADK pausa a execução com um `adk_request_confirmation` e só é retomada quando a resposta do morador (uma `FunctionResponse` com esse id) chega **ao mesmo agente que pediu a confirmação** — no código do ADK (`flows/llm_flows/tools/_confirmation.py`) o processador ignora pedidos autorados por outro agente. Antes de escrever a API, testei as combinações com sessão persistida em SQLite e **reiniciando o processo entre o pedido e a resposta**:

| Topologia | Retomada do App | Aprovar | Negar | Resultado |
| --- | --- | --- | --- | --- |
| concierge + `sub_agents` (transferência) | sem `ResumabilityConfig` | tool executa 1× | não executa | **escolhida** |
| concierge + `sub_agents` | `ResumabilityConfig(is_resumable=True)` + `invocation_id` | 1× | não executa | também funciona (mais peças, sem ganho aqui) |
| concierge com `AgentTool(especialista)` | — | — | — | **falha silenciosa**: o especialista roda numa sessão interna em memória; o pedido de confirmação nunca chega à sessão persistida e o concierge responde "solicitação encaminhada" |

Com `sub_agents`, o pedido de confirmação fica gravado na sessão com o especialista como autor, e o Runner, ao receber a `FunctionResponse`, escolhe exatamente esse agente para continuar — inclusive depois de reiniciar a API.

### Fluxo de uma confirmação

1. O morador pede "Reserve o salão de festas…" → o especialista chama `reservar_area`; como a área tem taxa, o ADK não executa a tool e emite `adk_request_confirmation`.
2. `api.py` captura esse evento e grava a pendência na tabela `confirmacoes` (id, sessão, ação e detalhes com área e data); a resposta da API traz `confirmacoes_pendentes`.
3. `POST /sessoes/{id}/confirmacoes` faz a transição atômica `pendente → respondida`; só então envia a `FunctionResponse` ao Runner, que a entrega ao especialista, e a tool executa (aprovado) ou não (negado).

### Persistência

- **Sessões e eventos do ADK:** `DatabaseSessionService` em `var/sessions.db` (SQLite via `aiosqlite`).
- **Dados do condomínio:** `var/aurora.db` (SQLite, WAL + `busy_timeout`), tabelas `apartamentos`, `areas`, `reservas`, `visitantes`, `sessoes`, `confirmacoes`.
- Os arquivos de `dados/` são só a fonte do estado inicial (lidos pela restauração; nunca alterados).

## Garantias

### Garantia 1 — cobrança ou acesso só com confirmação

**Onde:** `src/aurora/tools.py` (declaração das tools e defesa na própria tool), `src/aurora/db.py` (`responder_confirmacao`), `src/aurora/api.py` (rota de confirmações).

```python
# src/aurora/tools.py — a confirmação é exigida pelo ADK antes de executar a tool
def _reserva_precisa_confirmacao(area: str = "", data: str = "", **_) -> bool:
    """Reservar área com taxa > 0 gera cobrança → exige confirmação (regra 2)."""
    area_info = db.resolver_area(area)
    return bool(area_info and area_info["taxa"] > 0)
...
    FunctionTool(reservar_area, require_confirmation=_reserva_precisa_confirmacao),
...
    FunctionTool(autorizar_visitante, require_confirmation=True),
```

```python
# src/aurora/tools.py — defesa em profundidade dentro da tool (reservar_area e autorizar_visitante)
    confirmacao = tool_context.tool_confirmation
    if confirmacao is None or not confirmacao.confirmed:
        return {"ok": False, "autorizado": False,
                "erro": "Entrada NÃO liberada: a autorização não foi aprovada pelo sistema de confirmações. Nada foi gravado."}
```

```python
# src/aurora/db.py — a única porta para responder: UPDATE condicionado ao status
        cur = conn.execute(
            "UPDATE confirmacoes SET status = 'respondida', confirmado = ?"
            " WHERE id = ? AND session_id = ? AND status = 'pendente'",
            (1 if confirmado else 0, confirmacao_id, session_id))
        if cur.rowcount != 1:
            return None
```

```python
# src/aurora/api.py — sem transição → 409 e nada é enviado ao Runner
    function_call_id = db.responder_confirmacao(session_id, body.id, body.confirmado)
    if function_call_id is None:
        return JSONResponse(status_code=409, ...)
```

**Por que não depende do modelo:** a exigência de confirmação está na declaração da tool (`require_confirmation`), decidida por código a partir da taxa da área no banco; o modelo não tem como chamar a tool "já confirmada". Mensagens como "já estou confirmando aqui" são texto — a única forma de confirmar é a rota HTTP, que só aceita um id pendente daquela sessão, uma única vez (o `UPDATE … WHERE status = 'pendente'` é atômico: reenvio, id inexistente ou de outra sessão afetam 0 linhas → 409). Mesmo se o ADK entregasse uma negação como aprovação, a tool confere `tool_confirmation.confirmed` antes de gravar. A quadra (taxa 0) não pede confirmação porque o callable devolve `False`.

### Garantia 2 — cada sessão pertence a um apartamento

**Onde:** `src/aurora/api.py` (`Assistente.criar_sessao`), `src/aurora/tools.py` (`_apartamento` e todas as tools), `src/aurora/db.py` (consultas filtradas).

```python
# src/aurora/api.py — gravado UMA vez, na criação da sessão (state + tabela `sessoes`)
        await self.sessoes.create_session(app_name=config.APP_NAME, user_id=self.user_id(apartamento),
                                          session_id=session_id, state={"apartamento": apartamento})
        db.registrar_sessao(session_id, apartamento)
```

```python
# src/aurora/tools.py — toda tool resolve o apartamento pela sessão; nenhuma tool tem parâmetro de apartamento
def _apartamento(tool_context: ToolContext) -> str:
    """Apartamento autenticado da sessão; nunca um valor escolhido pelo modelo."""
    session = tool_context.session
    apartamento = db.apartamento_da_sessao(session.id)
    if apartamento is None or session.state.get("apartamento") != apartamento:
        raise SessaoInvalida("Sessão sem apartamento válido.")
    return apartamento
```

```python
# src/aurora/tools.py — disponibilidade devolve só livre/ocupada, nunca o dono
    situacao = "livre" if db.data_livre(area_info["id"], data_ok) else "ocupada"
```

```python
# src/aurora/db.py — cancelamento só de reserva ATIVA do próprio apartamento
                "SELECT id, codigo FROM reservas WHERE area = ? AND data = ? AND apartamento = ? AND status = 'ativa'",
```

**Por que não depende do modelo:** as assinaturas das tools (`listar_minhas_reservas()`, `reservar_area(area, data)`, `cancelar_minha_reserva(codigo | area + data)`, `autorizar_visitante(nome, data)`…) não têm campo de apartamento — mesmo que o morador diga "sou do 302", o modelo não tem onde colocar esse número. Todas as consultas filtram pelo apartamento da sessão; um cancelamento de reserva alheia retorna "nenhuma reserva do seu apartamento corresponde" sem revelar se ela existe; e nenhuma tool devolve código, nome ou apartamento de terceiros.

### Garantia 3 — nada se perde no reinício

**Onde:** `src/aurora/api.py` e `src/aurora/config.py`.

```python
# src/aurora/api.py — sessões e eventos do ADK em arquivo
        self.sessoes = DatabaseSessionService(db_url=config.SESSIONS_DB_URL, connect_args={"timeout": 30})
        self.runner = Runner(app=App(name=config.APP_NAME, root_agent=build_root_agent()),
                             session_service=self.sessoes)
```

```python
# src/aurora/config.py
DB_PATH = VAR_DIR / "aurora.db"
SESSIONS_DB_PATH = VAR_DIR / "sessions.db"
SESSIONS_DB_URL = f"sqlite+aiosqlite:///{SESSIONS_DB_PATH}"
```

**Por que não depende do modelo:** eventos, state e pendências ficam em SQLite em disco (`var/`); a API não guarda nada só em memória além de locks por sessão. Ao subir, `db.garantir_dados()` só cria o schema (não restaura), então reiniciar a API mantém reservas, visitantes, sessões e confirmações; a retomada de confirmação funciona após reinício (testado no spike e no passo 13 do fluxo).

### Garantia 4 — o regulamento é consultado, não carregado

**Onde:** `src/aurora/regulamento.py`, `src/aurora/tools.py` (`consultar_regulamento`) e `src/aurora/agents.py`.

```python
# src/aurora/regulamento.py — escolhe UM capítulo por palavras-chave (título + sinônimos)
def buscar(assunto: str) -> Capitulo | None:
    """Escolhe UM capítulo pela maior quantidade de palavras-chave presentes no assunto."""
```

```python
# src/aurora/tools.py — a tool devolve só o texto desse capítulo
    return {"ok": True, "capitulo": f"Capítulo {capitulo.numero}: {capitulo.titulo}", "texto": capitulo.texto}
```

```python
# src/aurora/agents.py — o especialista conhece só os títulos; o concierge não recebe nada do regulamento
Índice do regulamento (apenas títulos; o texto vem da tool):
{regulamento.indice()}
```

**Por que não depende do modelo:** o regulamento nunca entra inteiro em instruções nem em eventos — o único texto do regulamento que chega à sessão é o retorno da tool, que é recortado em código por capítulo (`## Capítulo …`). Para "Até que horas a piscina funciona aos domingos?" a tool devolve apenas o Capítulo IV (Art. 22, II: das 9h às 20h); os demais capítulos não aparecem em nenhum evento.

### Garantia 5 — dois moradores, uma reserva

**Onde:** `src/aurora/db.py` (schema e `criar_reserva`).

```sql
-- src/aurora/db.py
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa ON reservas(area, data) WHERE status = 'ativa';
```

```python
# src/aurora/db.py — o INSERT é a fonte da verdade; a violação vira resposta normal
def criar_reserva(apartamento: str, area_id: str, data: str) -> str:
    """Grava a reserva; a exclusividade é decidida pelo índice único parcial no INSERT."""
    try:
        with transacao() as conn:
            cur = conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, status) VALUES (?, ?, ?, ?, 'ativa')", ...)
            codigo = f"{CODIGO_PREFIXO}{cur.lastrowid:04d}"
            ...
    except sqlite3.IntegrityError as exc:
        if "reservas.area" in str(exc) or "ux_reserva_ativa" in str(exc):
            raise DataIndisponivel() from exc
```

**Por que não depende do modelo:** a exclusividade é decidida pelo banco no instante da gravação — duas transações simultâneas (`BEGIN IMMEDIATE`, WAL, `busy_timeout`) não conseguem gravar duas reservas ativas para a mesma área e data, independentemente de qualquer checagem anterior. A perdedora recebe `IntegrityError`, que a tool transforma em "já está ocupado(a)" (HTTP 200). Cancelar muda o status (não apaga), e o código vem de um `AUTOINCREMENT` com prefixo próprio (`RSV-A0001…`) e `UNIQUE` em todas as linhas — nunca repete um código existente, cancelado ou inicial (`RSV-1377`, `RSV-4821`, `RSV-2950`), nem depois de restaurar.

## Como rodar

### Pré-requisitos

- Python 3.12+ e [uv](https://docs.astral.sh/uv/)
- Chave do Google AI Studio: https://aistudio.google.com/app/apikey (o fluxo completo faz algumas dezenas de chamadas; no plano gratuito o limite diário por modelo pode não ser suficiente)

### Configuração

```bash
uv sync
cp .env.example .env    # preencha GOOGLE_API_KEY
```

| Variável | Obrigatória | Descrição |
| --- | --- | --- |
| `GOOGLE_API_KEY` | sim | chave do Google AI Studio |
| `GOOGLE_GENAI_USE_VERTEXAI` | sim | `False` (usa o AI Studio) |
| `AURORA_MODEL` | não | modelo Gemini dos agentes (padrão `gemini-3.8-flash`) |

Não há serviço externo: os bancos são arquivos SQLite em `var/` (criados automaticamente, fora do Git).

### Restaurar os dados iniciais

```bash
uv run aurora-restaurar
```

Volta reservas e visitantes ao estado de `dados/*.json` e **também apaga as sessões e confirmações** (estado inicial limpo). Rode com a API parada.

### Subir a API

```bash
uv run aurora-api
```

A API responde em `http://localhost:8000`. Na primeira subida, se o banco não existir, os dados iniciais são carregados automaticamente; nas seguintes, os dados e sessões existentes são mantidos.

### Exemplo

```bash
curl -s localhost:8000/apartamentos/101/reservas
SID=$(curl -s -X POST localhost:8000/sessoes -H 'Content-Type: application/json' -d '{"apartamento":"101"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessoes/$SID/mensagens -H 'Content-Type: application/json' -d '{"texto":"Reserve o salão de festas para 2030-04-20"}'
# → confirmacoes_pendentes: [{"id": "...", "acao": "reservar_area_com_taxa", "detalhes": {"area": "salao-de-festas", "data": "2030-04-20", "taxa": 150.0}}]
curl -s -X POST localhost:8000/sessoes/$SID/confirmacoes -H 'Content-Type: application/json' -d '{"id":"<id>","confirmado":true}'
curl -s localhost:8000/sessoes/$SID/eventos
```

### Rotas

| Rota | Resposta |
| --- | --- |
| `POST /sessoes` `{"apartamento"}` | `201 {"session_id"}` |
| `POST /sessoes/{id}/mensagens` `{"texto"}` | `200 {"resposta", "confirmacoes_pendentes"}` · `404` sessão inexistente |
| `POST /sessoes/{id}/confirmacoes` `{"id", "confirmado"}` | `200` mesmo formato · `409` sem pendente com esse id nesta sessão · `404` |
| `GET /sessoes/{id}/eventos` | `200` todos os eventos, em ordem · `404` |
| `GET /apartamentos/{n}/reservas` | `200 [{"codigo", "area", "data"}]` (ativas) |
| `GET /apartamentos/{n}/visitantes` | `200 [{"nome", "data"}]` |
