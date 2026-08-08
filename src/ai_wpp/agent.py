"""LangGraph agent wrapping a Mistral chat model with per-user memory.

Design notes:

* The system prompt is a **Markdown template** with mustache-style
  placeholders (``{{var}}``). It is loaded once and rendered directly inside
  the LangGraph node, without a LangChain prompt chain.
* Per-turn runtime context (current timestamp, appropriate greeting,
  contact display name, first-turn flag) is injected by
  :func:`_build_prompt_context` and substituted at invocation time.
* Each WhatsApp sender gets an isolated conversation identified by their
  phone number (used as the LangGraph ``thread_id``). State is persisted
  through an ``AsyncSqliteSaver`` checkpointer so restarts don't forget.
* History is trimmed on every turn so we never send an unbounded prompt
  to the model. The limit is configurable via ``AGENT_HISTORY_WINDOW``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_mistralai import ChatMistralAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

_BR_TZ = ZoneInfo("America/Sao_Paulo")
_PROMPT_VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")

_WEEKDAY_PT = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)

_MONTH_PT = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def _load_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"System prompt not found at {path}. Set AGENT_PROMPT_PATH correctly."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"System prompt file {path} is empty.")
    return text


def _render_prompt(template: str, context: dict[str, str]) -> str:
    """Render the prompt's simple ``{{variable}}`` placeholders.

    The project only needs scalar variable replacement, so keeping rendering
    local avoids building a separate LangChain prompt pipeline around the
    LangGraph node. Unknown variables fail fast instead of leaking into the
    model input.
    """
    variables = set(_PROMPT_VARIABLE_RE.findall(template))
    missing = variables.difference(context)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Missing system prompt variables: {names}")

    return _PROMPT_VARIABLE_RE.sub(lambda match: context[match.group(1)], template)


def _build_model(settings: Settings) -> BaseChatModel:
    return ChatMistralAI(
        model=settings.mistral_model,
        mistral_api_key=settings.mistral_api_key.get_secret_value(),
        temperature=settings.mistral_temperature,
        max_tokens=settings.mistral_max_tokens,
        timeout=60,
    )


def _saudacao_for(hour: int) -> str:
    if 5 <= hour < 12:
        return "Bom dia"
    if 12 <= hour < 18:
        return "Boa tarde"
    return "Boa noite"


def _format_current_datetime(now: datetime) -> str:
    weekday = _WEEKDAY_PT[now.weekday()]
    month = _MONTH_PT[now.month - 1]
    return (
        f"{weekday}, {now.day} de {month} de {now.year}, "
        f"{now:%H:%M} (horário de Brasília)"
    )


def _build_prompt_context(
    *, history_len: int, contact_name: str, now: datetime | None = None
) -> dict[str, str]:
    current = now or datetime.now(_BR_TZ)
    return {
        "current_datetime": _format_current_datetime(current),
        "saudacao": _saudacao_for(current.hour),
        "contact_name": contact_name or "",
        # Use "sim"/"não" so the model reasons about the variable as
        # intended by the prompt copy.
        "is_first_turn": "sim" if history_len <= 1 else "não",
    }


class WhatsAppAgent:
    """High-level façade used by the webhook handler.

    Call :meth:`setup` once at application startup, then :meth:`reply` per
    inbound message. Call :meth:`aclose` at shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = _build_model(settings)
        self._checkpointer_ctx: Any | None = None
        self._graph: Any | None = None
        self._lock = asyncio.Lock()
        self._system_prompt = _load_prompt(settings.agent_prompt_path)

    async def setup(self) -> None:
        """Open the SQLite checkpointer and compile the graph."""
        settings = self._settings
        settings.agent_checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

        self._checkpointer_ctx = AsyncSqliteSaver.from_conn_string(
            str(settings.agent_checkpoint_db)
        )
        checkpointer = await self._checkpointer_ctx.__aenter__()

        builder = StateGraph(MessagesState)
        builder.add_node("chat", self._chat_node)
        builder.add_edge(START, "chat")
        builder.add_edge("chat", END)
        self._graph = builder.compile(checkpointer=checkpointer)

        log.info(
            "agent.ready",
            model=settings.mistral_model,
            history_window=settings.agent_history_window,
            checkpoint_db=str(settings.agent_checkpoint_db),
        )

    async def aclose(self) -> None:
        if self._checkpointer_ctx is not None:
            await self._checkpointer_ctx.__aexit__(None, None, None)
            self._checkpointer_ctx = None
            self._graph = None

    async def reply(
        self, *, user_id: str, user_text: str, contact_name: str | None = None
    ) -> str:
        """Run one conversation turn and return the assistant text."""
        if self._graph is None:
            raise RuntimeError("Agent not initialized — call setup() first.")

        config: RunnableConfig = {
            "configurable": {
                "thread_id": user_id,
                "contact_name": contact_name or "",
            }
        }
        result = await self._graph.ainvoke(
            {"messages": [HumanMessage(content=user_text)]},
            config=config,
        )

        for message in reversed(result["messages"]):
            if isinstance(message, AIMessage):
                return _coerce_text(message.content).strip()
        return ""

    # ---- graph nodes ----
    async def _chat_node(
        self, state: MessagesState, config: RunnableConfig
    ) -> dict[str, list[AnyMessage]]:
        history: list[AnyMessage] = list(state["messages"])

        window = self._settings.agent_history_window
        trimmed = history[-window:] if len(history) > window else history

        configurable = (config or {}).get("configurable", {}) or {}
        contact_name = str(configurable.get("contact_name") or "")

        context = _build_prompt_context(
            history_len=len(history),
            contact_name=contact_name,
        )

        system_prompt = _render_prompt(self._system_prompt, context)
        messages: list[AnyMessage] = [
            SystemMessage(content=system_prompt),
            *trimmed,
        ]

        async with self._lock:
            # Mistral's free tier rate-limits hard; serialize outbound calls
            # to keep a predictable QPS from this process.
            response = await self._model.ainvoke(messages)

        return {"messages": [response]}


def _coerce_text(content: Any) -> str:
    """Flatten whatever an AIMessage.content may be into a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                piece = chunk.get("text") or chunk.get("content") or ""
                if isinstance(piece, str):
                    parts.append(piece)
        return "".join(parts)
    return str(content)
