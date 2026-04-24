"""FastAPI entrypoint. Receives W-API webhooks, runs the agent, replies."""

from __future__ import annotations

import hmac
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from cachetools import TTLCache
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import WhatsAppAgent
from .config import Settings, get_settings
from .logging import configure_logging, get_logger
from .state import ConversationStateStore
from .wapi import WApiClient, WApiError
from .webhook import InboundMessage, parse_inbound

log = get_logger(__name__)


# Dedupe inbound message IDs for 10 minutes — W-API may retry delivery if
# we reply slowly, and we must never run the agent twice for the same turn.
_DEDUPE_CACHE: TTLCache[str, bool] = TTLCache(maxsize=10_000, ttl=600)

# Track the messageIds we (the bot) produced, so the fromMe echo of our own
# replies doesn't get mistaken for a human takeover. 10 min is plenty —
# W-API delivers the echo in seconds.
_OUTBOUND_CACHE: TTLCache[str, bool] = TTLCache(maxsize=10_000, ttl=600)

# A line containing only "---" (optionally whitespace around) separates
# WhatsApp bubbles in the agent's output.
_BUBBLE_SEPARATOR = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*$")

# Delay (seconds) applied between consecutive bubbles to simulate a natural
# typing cadence. W-API accepts 1–15; 2s feels like a person typing.
_BUBBLE_DELAY_SECONDS = 2


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 — FastAPI signature
    settings = get_settings()
    configure_logging(settings.log_level)

    agent = WhatsAppAgent(settings)
    await agent.setup()

    wapi = WApiClient(settings)

    state_store = ConversationStateStore(settings.agent_state_db)
    await state_store.setup()

    app.state.settings = settings
    app.state.agent = agent
    app.state.wapi = wapi
    app.state.state_store = state_store

    log.info(
        "app.startup",
        model=settings.mistral_model,
        instance=settings.wapi_instance_id,
        takeover_window_seconds=settings.takeover_window_seconds,
    )
    try:
        yield
    finally:
        await wapi.aclose()
        await agent.aclose()
        await state_store.close()
        log.info("app.shutdown")


app = FastAPI(
    title="ai-wpp",
    version="0.1.0",
    lifespan=lifespan,
    # The only public endpoint is the webhook; hide interactive docs in prod.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# Host allow-list — set TRUSTED_HOSTS="bot.example.com,foo.example.com".
# Leave unset to allow any Host header (e.g. behind a trusted proxy).
_trusted_hosts_env = os.getenv("TRUSTED_HOSTS", "").strip()
if _trusted_hosts_env:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[h.strip() for h in _trusted_hosts_env.split(",") if h.strip()],
    )


@app.middleware("http")
async def _request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Any]]
) -> Any:
    """Bind a per-request ID and path to structlog, echo it back as a header."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["X-Request-ID"] = request_id
    return response


def _verify_secret(settings: Settings, provided: str | None) -> None:
    expected = settings.webhook_secret.get_secret_value()
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("webhook.unauthorized")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")


def _sender_allowed(settings: Settings, phone: str) -> bool:
    allowed = settings.allowed_sender_set
    return not allowed or phone in allowed


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/wapi")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
    token_qs: str | None = Query(default=None, alias="token"),
) -> JSONResponse:
    settings: Settings = request.app.state.settings

    # Accept the shared secret either via header or query string — W-API's
    # panel lets you configure the webhook URL but not custom headers on
    # every plan, so the query fallback is a pragmatic concession.
    _verify_secret(settings, x_webhook_token or token_qs)

    try:
        payload: dict[str, Any] = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body") from None

    inbound = parse_inbound(payload)
    if inbound is None:
        # Not a text message we care about. Ack so W-API stops retrying.
        log.debug("webhook.ignored", keys=list(payload.keys()))
        return JSONResponse({"status": "ignored"})

    if inbound.is_group:
        log.debug("webhook.skip_group", message_id=inbound.message_id)
        return JSONResponse({"status": "skip_group"})

    state_store: ConversationStateStore = request.app.state.state_store

    # fromMe = message sent from this WhatsApp account. Two sub-cases:
    #   (a) the bot replying — we track those IDs and must ignore the echo;
    #   (b) the real human (number owner) typing — that's a human takeover.
    if inbound.from_me:
        if inbound.message_id in _OUTBOUND_CACHE:
            log.debug("webhook.bot_echo", message_id=inbound.message_id)
            return JSONResponse({"status": "bot_echo"})
        return await _handle_human_takeover(settings, state_store, inbound)

    if not _sender_allowed(settings, inbound.phone):
        log.info("webhook.sender_not_allowed", phone=_mask(inbound.phone))
        return JSONResponse({"status": "forbidden"})

    if inbound.message_id in _DEDUPE_CACHE:
        log.info("webhook.duplicate", message_id=inbound.message_id)
        return JSONResponse({"status": "duplicate"})
    _DEDUPE_CACHE[inbound.message_id] = True

    # The human may have previously taken over this conversation. If the
    # window is still active (or an explicit pause is set), don't reply.
    if await state_store.is_muted(inbound.phone):
        log.info(
            "webhook.muted",
            message_id=inbound.message_id,
            phone=_mask(inbound.phone),
        )
        return JSONResponse({"status": "muted"})

    # Ack fast, do the LLM call in the background. W-API webhooks have a
    # short timeout — returning in <1s is the right shape.
    background.add_task(_process_message, request.app, inbound)
    return JSONResponse({"status": "accepted"})


async def _handle_human_takeover(
    settings: Settings,
    state_store: ConversationStateStore,
    inbound: InboundMessage,
) -> JSONResponse:
    """React to a message the instance owner typed in WhatsApp directly.

    The bot's own echoed replies are filtered out *before* this runs.
    """
    command = _classify_operator_command(settings, inbound.text)
    phone = inbound.phone
    masked = _mask(phone)

    if command == "resume":
        await state_store.resume(phone)
        log.info("takeover.resumed", phone=masked)
        return JSONResponse({"status": "resumed"})

    if command == "pause":
        await state_store.mute(phone, duration_seconds=None)
        log.info("takeover.paused_indefinite", phone=masked)
        return JSONResponse({"status": "paused_indefinite"})

    if settings.takeover_window_seconds <= 0:
        # Auto-mute window disabled by config — only explicit commands mute.
        log.debug("takeover.window_disabled", phone=masked)
        return JSONResponse({"status": "takeover_window_disabled"})

    await state_store.mute(phone, duration_seconds=settings.takeover_window_seconds)
    log.info(
        "takeover.window_started",
        phone=masked,
        seconds=settings.takeover_window_seconds,
    )
    return JSONResponse({"status": "human_takeover"})


def _classify_operator_command(settings: Settings, text: str) -> str | None:
    """Return ``"pause"``, ``"resume"`` or ``None`` for a human-authored text."""
    normalized = text.strip().lower()
    if not normalized:
        return None
    if normalized in settings.takeover_pause_command_set:
        return "pause"
    if normalized in settings.takeover_resume_command_set:
        return "resume"
    return None


async def _process_message(app: FastAPI, inbound: InboundMessage) -> None:
    agent: WhatsAppAgent = app.state.agent
    wapi: WApiClient = app.state.wapi

    log_ctx = log.bind(
        message_id=inbound.message_id,
        phone=_mask(inbound.phone),
        sender=inbound.sender_name,
    )
    log_ctx.info("agent.turn.start", chars=len(inbound.text))

    try:
        answer = await agent.reply(
            user_id=inbound.phone,
            user_text=inbound.text,
            contact_name=inbound.sender_name,
        )
    except Exception:  # noqa: BLE001 — we want to log & degrade gracefully
        log_ctx.exception("agent.turn.failed")
        answer = (
            "Deixa eu retomar aqui do meu lado, me manda de novo em um "
            "instantinho? 💛"
        )

    bubbles = _split_bubbles(answer)
    if not bubbles:
        log_ctx.warning("agent.empty_reply")
        return

    for idx, bubble in enumerate(bubbles):
        try:
            sent = await wapi.send_text(
                phone=inbound.phone,
                message=bubble,
                # Only the first bubble quotes the user's message; the rest
                # are continuations in the same thread.
                reply_to=inbound.message_id if idx == 0 else None,
                # Let W-API simulate natural typing between bubbles.
                delay_seconds=_BUBBLE_DELAY_SECONDS if idx > 0 else None,
            )
        except WApiError:
            log_ctx.exception("wapi.send_failed", bubble_index=idx)
            return

        sent_id = sent.get("messageId")
        if isinstance(sent_id, str) and sent_id:
            _OUTBOUND_CACHE[sent_id] = True

    log_ctx.info(
        "agent.turn.done",
        bubbles=len(bubbles),
        total_chars=sum(len(b) for b in bubbles),
    )


def _split_bubbles(text: str) -> list[str]:
    """Split the model's answer into one or more WhatsApp bubbles.

    The separator is a line containing only three or more dashes. Empty
    segments are dropped.
    """
    if not text:
        return []
    parts = _BUBBLE_SEPARATOR.split(text)
    return [part.strip() for part in parts if part.strip()]


def _mask(phone: str) -> str:
    """Mask the middle digits of a phone number for logs (LGPD-friendly)."""
    if len(phone) <= 6:
        return phone
    return f"{phone[:4]}***{phone[-2:]}"
