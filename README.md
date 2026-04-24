# ai-wpp

Agente de IA para **WhatsApp** construído com **LangGraph + Mistral** e integrado à
[W-API](https://docs.w-api.app/). O prompt do agente fica em um arquivo Markdown
separado ([`prompts/system.md`](prompts/system.md)) — edite sem tocar no código.

## Arquitetura

```
WhatsApp  ──►  W-API  ──►  POST /webhook/wapi  (FastAPI)
                                │
                                ▼
                    parse + dedupe + auth
                                │
                                ▼
                LangGraph agent  ◄──►  Mistral API
                                │          (chat model)
                                ▼
                    memória por usuário
                    (SQLite checkpointer,
                     thread_id = telefone)
                                │
                                ▼
                W-API  POST /message/send-text
                                │
                                ▼
                           WhatsApp
```

Principais características:

- **Prompt externo em Markdown** — editável sem deploy.
- **Memória isolada por usuário** via `AsyncSqliteSaver` do LangGraph (o
  `thread_id` é o telefone do remetente).
- **Idempotência** — cada `messageId` do W-API é processado uma única vez
  (cache TTL de 10 min).
- **Resposta rápida ao webhook** — o LLM roda em background, o webhook
  responde em milissegundos (evita retry storm).
- **Parser tolerante** — o `webhook.py` aceita múltiplas variações do JSON
  que o W-API pode enviar (formato direto, aninhado em `data`, Baileys-style).
- **Segurança** — token compartilhado no header `X-Webhook-Token` (ou
  `?token=` se seu plano não permitir headers customizados), validado com
  `hmac.compare_digest`.
- **Retries + timeouts** no cliente W-API (`tenacity` + `httpx`).
- **Logs JSON estruturados** via `structlog`, com mascaramento de telefone
  (LGPD-friendly).

## Requisitos

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) para gerenciar deps (recomendado) ou `pip`
- Uma instância ativa no painel W-API (pareada via QR code) com o seu número
- Uma API key da Mistral

## Setup

```bash
# 1. clonar/entrar no diretório
cd ai_wpp

# 2. criar .env a partir do template
cp .env.example .env
# edite .env e preencha WAPI_TOKEN, WAPI_INSTANCE_ID, WEBHOOK_SECRET,
# MISTRAL_API_KEY

# 3. instalar deps
uv sync --all-extras
# ou: pip install -e ".[dev]"

# 4. rodar em dev
make dev
# ou: uv run uvicorn ai_wpp.main:app --reload
```

O servidor sobe em `http://0.0.0.0:8000`. Endpoints:

- `GET  /health` — healthcheck simples.
- `POST /webhook/wapi` — recebe os eventos do W-API.

## Configurar o webhook no W-API

1. Exponha o servidor publicamente (use ngrok/cloudflared em dev):
   ```bash
   ngrok http 8000
   ```
2. No painel W-API, vá em **Instâncias → sua instância → Configurações →
   Webhook** e aponte para:
   ```
   https://SEU-DOMINIO/webhook/wapi?token=O_MESMO_VALOR_DO_WEBHOOK_SECRET
   ```
   Alternativamente, se o painel permitir headers customizados, envie
   `X-Webhook-Token: O_MESMO_VALOR`. O servidor aceita os dois.
3. Ative os eventos de **mensagem recebida** (incoming message).

## Editar o comportamento do agente

Todo o comportamento do agente vive em [`prompts/system.md`](prompts/system.md).
Edite, salve, reinicie o servidor. Não precisa tocar em código.

Para trocar o modelo (ex.: usar `mistral-medium-latest` para reduzir custo em
dev), basta alterar `MISTRAL_MODEL` no `.env`.

### Template — variáveis injetadas por turno

O prompt é compilado como um `ChatPromptTemplate` do LangChain em formato
**mustache**. As variáveis abaixo são renderizadas fresh a cada mensagem:

| Variável              | Valor em runtime                                                           |
| --------------------- | -------------------------------------------------------------------------- |
| `{{current_datetime}}`| Ex.: *"quinta-feira, 23 de abril de 2026, 16:45 (horário de Brasília)"*    |
| `{{saudacao}}`        | `Bom dia` (5–11h), `Boa tarde` (12–17h) ou `Boa noite` (18–4h), TZ BR       |
| `{{contact_name}}`    | `pushName` do WhatsApp quando existir; senão, string vazia                 |
| `{{is_first_turn}}`   | `"sim"` no primeiro turno da conversa; `"não"` a partir do segundo          |

Isso permite que o prompt use saudação correta sem "Bom dia" às 22h, personalize
pelo nome quando disponível, e execute uma abertura diferente no primeiro turno.

### Respostas em múltiplas bolhas

O prompt pode separar a resposta em várias mensagens do WhatsApp usando uma
linha contendo apenas `---` como separador. O servidor quebra nesses pontos e
envia cada bolha separadamente, com um delay natural de 2 s entre elas
(usando o parâmetro `delayMessage` do W-API). A primeira bolha é enviada como
reply à mensagem original do cliente; as seguintes como mensagens regulares.

Exemplo de saída do modelo:

```
Oii!! Bom dia! Aqui é a Clara, da equipe da Marih, tudo bem?
---
Pra eu poder te ajudar melhor, me conta: sobre o que você precisa?
```

→ dois envios sequenciais ao mesmo número.

## Controlar quem pode falar com o bot

Por padrão qualquer número pode iniciar uma conversa. Para restringir:

```env
ALLOWED_SENDERS=5511999999999,5511888888888
```

Números devem estar em formato E.164 **apenas com dígitos** (sem `+`).

## Human handoff (takeover)

O dono do número pode entrar na conversa a qualquer momento e a IA sai de
cena automaticamente.

**Como funciona:**

- Quando o dono do número digita uma mensagem no WhatsApp, o W-API entrega
  um webhook com `fromMe=true`. O servidor identifica isso como takeover e
  **muta** a IA para aquela conversa específica — as outras conversas
  seguem normais.
- A mensagem que o próprio bot envia também chega de volta como
  `fromMe=true`. Não há confusão: o servidor guarda o `messageId` de cada
  resposta que ele mesmo produziu e reconhece o echo.
- O mute dura `TAKEOVER_WINDOW_SECONDS` (default 30 min) a partir da
  **última** mensagem do humano. Se ele continuar conversando, o timer se
  renova. Quando ele para, a IA volta sozinha.
- Comandos explícitos (escritos pelo próprio dono no chat):
  - `!pausar` (ou `!pause`, `!ai off`, `!bot off`) — muta **indefinidamente**.
  - `!retomar` (ou `!resume`, `!ai on`, `!bot on`) — devolve o controle agora.
  - Comandos são exatos, case-insensitive. Configure em
    `TAKEOVER_PAUSE_COMMANDS` / `TAKEOVER_RESUME_COMMANDS`.
- `TAKEOVER_WINDOW_SECONDS=0` desliga o auto-mute (só comandos mutam).

**Estado persistente:** guardado em `data/state.sqlite`. Sobrevive a
restart e redeploy (monte o mesmo volume que `data/checkpoints.sqlite`).

**Logs de observabilidade** — filtre por:

- `event="takeover.window_started"` — humano entrou, mute temporário.
- `event="takeover.paused_indefinite"` — comando `!pausar` acionado.
- `event="takeover.resumed"` — comando `!retomar` acionado.
- `event="webhook.muted"` — mensagem do cliente ignorada porque o humano
  está no controle.
- `event="webhook.bot_echo"` — echo de resposta própria, ignorado.

## Memória de conversa

Cada telefone tem uma conversa isolada persistida em
`data/checkpoints.sqlite`. A janela de histórico usada no prompt é controlada
por `AGENT_HISTORY_WINDOW` (default: 30 mensagens). Para "esquecer" um
usuário, apague seu `thread_id` do banco ou remova o arquivo SQLite inteiro.

## Estrutura do projeto

```
ai_wpp/
├── prompts/
│   └── system.md            # prompt do agente (edite aqui)
├── src/ai_wpp/
│   ├── agent.py             # LangGraph + Mistral + checkpointer
│   ├── config.py            # pydantic-settings (.env)
│   ├── logging.py           # structlog JSON
│   ├── main.py              # FastAPI app + webhook + middlewares
│   ├── wapi.py              # cliente HTTP W-API (httpx + tenacity)
│   └── webhook.py           # parser tolerante do payload W-API
├── data/                    # SQLite do checkpointer (gitignored)
├── Dockerfile               # imagem de produção (multi-stage, non-root)
├── docker-compose.yml       # deploy single-host com volume persistente
├── fly.toml                 # deploy Fly.io
├── .dockerignore
├── .env.example
├── pyproject.toml
├── Makefile
└── README.md
```

## Deploy

Três caminhos, do mais rápido ao mais flexível. Qualquer um deles produz um
servidor HTTPS estável ao qual você aponta o webhook do painel W-API.

> **Regra única:** rode com `--workers 1`. O checkpointer SQLite e o cache
> de deduplicação vivem em memória/arquivo do processo. Para escalar
> horizontalmente, migre o checkpointer para Postgres
> (`langgraph-checkpoint-postgres`) e o dedupe para Redis — aí `--workers`
> deixa de ser um limite.

### Antes de qualquer deploy

```bash
# 1. Gere o lockfile (commitar junto com o código para builds reproduzíveis).
uv lock

# 2. Defina um WEBHOOK_SECRET forte (nunca reutilize entre ambientes).
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Opção 1 — Fly.io (recomendado)

Melhor combinação de simplicidade, latência (região `gru`), TLS automático e
volume persistente barato.

```bash
fly launch --no-deploy                      # edite o app name em fly.toml
fly volumes create ai_wpp_data --region gru --size 1

fly secrets set \
  WAPI_TOKEN="..." \
  WAPI_INSTANCE_ID="..." \
  WEBHOOK_SECRET="..." \
  MISTRAL_API_KEY="..." \
  TRUSTED_HOSTS="seu-app.fly.dev"

fly deploy
fly logs                                    # acompanhe em tempo real
```

Webhook no painel W-API:
`https://seu-app.fly.dev/webhook/wapi?token=<WEBHOOK_SECRET>`

### Opção 2 — Docker Compose (single-host / VPS)

Pra quando você quer controle total em uma VM (Hetzner, AWS EC2, etc.).

```bash
# Na VM:
cp .env.example .env   # preencha tudo
docker compose up -d --build
docker compose logs -f
```

Coloque **Caddy** (ou Traefik/Nginx) na frente para TLS e roteamento:

```caddy
bot.seudominio.com {
    reverse_proxy 127.0.0.1:8000
}
```

Depois atualize no `.env` da VM: `TRUSTED_HOSTS=bot.seudominio.com`.

### Opção 3 — Qualquer runtime de container (Cloud Run, ECS, Render)

O `Dockerfile` é autocontido. Variáveis obrigatórias:

| Var                 | Obrigatória | Notas |
| ------------------- | :---------: | ----- |
| `WAPI_TOKEN`        | ✅           | Bearer do painel W-API |
| `WAPI_INSTANCE_ID`  | ✅           | ID da instância pareada |
| `WEBHOOK_SECRET`    | ✅           | ≥ 32 chars, random |
| `MISTRAL_API_KEY`   | ✅           | chave Mistral |
| `MISTRAL_MODEL`     | ⬜           | default `mistral-large-latest` |
| `AGENT_CHECKPOINT_DB` | ⬜         | aponte para um volume persistente |
| `TRUSTED_HOSTS`     | ⬜           | CSV de hosts aceitos |
| `ALLOWED_SENDERS`   | ⬜           | allow-list de telefones |
| `LOG_LEVEL`         | ⬜           | default `INFO` |

Porta exposta: `8000`. Healthcheck: `GET /health`. Monte um volume em
`/app/data` pra não perder a memória da conversa a cada redeploy.

> **Cloud Run / Lambda:** não servem este caso — filesystem efêmero e
> escalagem a zero quebram a memória persistente. Se for inevitável, troque
> o checkpointer por Postgres antes.

### Pós-deploy — checklist de validação

1. `curl https://seu-host/health` → `{"status":"ok"}`.
2. Enviar webhook falso sem token → deve retornar 401.
3. Enviar uma mensagem real ao número e acompanhar os logs:
   `agent.turn.start → agent.turn.done` em poucos segundos.
4. Reiniciar o serviço e confirmar que o histórico da conversa persiste
   (faça uma pergunta que dependa do turno anterior).
5. Medir tempo do webhook até o ACK: deve ficar abaixo de ~200ms (o LLM
   roda em background, o webhook só enfileira).

### Custos esperados (ordem de grandeza)

- Fly.io `shared-cpu-1x 512MB` + volume 1GB: **~US$ 3–5/mês** ocioso.
- Mistral `mistral-large-latest`: paga por token. Uma conversa típica de
  20 turnos cabe bem abaixo de US$ 0,05. Monitore em
  [console.mistral.ai](https://console.mistral.ai/).
- W-API: plano da sua conta (externo).

## Observabilidade

Logs saem em JSON no stdout, um evento por linha. Campos úteis para filtrar:

- `event="agent.turn.start|done|failed"`
- `event="wapi.send_failed"`
- `event="webhook.duplicate|sender_not_allowed|skip_group"`

Plugue em Loki/Datadog/CloudWatch direto.

## Segurança — checklist

- [x] Token de webhook validado com `hmac.compare_digest`
- [x] Secrets em `SecretStr` (nunca caem em logs por acidente)
- [x] Telefone mascarado nos logs
- [x] Allow-list opcional de remetentes (`ALLOWED_SENDERS`)
- [x] Allow-list opcional de `Host` header (`TRUSTED_HOSTS`)
- [x] Idempotência por `messageId`
- [x] Timeouts + retries no cliente W-API
- [x] Container não-root, read-only FS, `no-new-privileges`, capabilities dropadas
- [x] `/docs`, `/redoc` e `/openapi.json` desabilitados em produção
- [x] `X-Request-ID` propagado nos logs e na resposta
- [ ] Rate-limit por remetente (TODO — adicione um middleware se precisar)
- [ ] Assinatura HMAC do corpo do webhook (TODO — depende de suporte no W-API)

## Extensões comuns

- **Adicionar ferramentas ao agente** — troque o nó `chat` por
  `langgraph.prebuilt.create_react_agent(model, tools=[...])`.
- **Outro provedor de LLM** — substitua `ChatMistralAI` em `agent.py`.
- **Trocar SQLite por Postgres** — use `AsyncPostgresSaver` do pacote
  `langgraph-checkpoint-postgres`.
- **Mensagens em grupo** — remova o guard `is_group` no `main.py` e ajuste o
  `thread_id` para incluir o JID do grupo.

## Licença

Proprietário.
