"""Inbound webhook payload parsing for W-API.

The exact JSON shape varies slightly between W-API plans and versions. This
module normalizes whatever comes in into a single :class:`InboundMessage`
struct that the rest of the app can depend on. If a field we do not care
about is missing or renamed, we simply ignore it — we never crash the
webhook handler on schema drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DIGITS_RE = re.compile(r"\D+")


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Normalized representation of an inbound WhatsApp text message."""

    message_id: str
    phone: str  # Sender phone, digits only.
    text: str
    from_me: bool
    is_group: bool
    sender_name: str | None
    instance_id: str | None
    raw: dict[str, Any]


def _first(d: dict[str, Any], *keys: str) -> Any:
    """Return the first non-empty value found for any of `keys` (dotted paths allowed)."""
    for key in keys:
        node: Any = d
        found = True
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                found = False
                break
        if found and node not in (None, "", [], {}):
            return node
    return None


def _extract_text(payload: dict[str, Any]) -> str | None:
    """Pull the visible text out of the many shapes W-API may send."""
    # Direct shapes
    direct = _first(
        payload,
        "text",
        "body",
        "message",
        "message.text",
        "message.body",
        "message.conversation",
        "msgContent.conversation",
        "msgContent.extendedTextMessage.text",
        "data.message.conversation",
        "data.message.extendedTextMessage.text",
        "data.msgContent.conversation",
        "data.msgContent.extendedTextMessage.text",
    )
    if isinstance(direct, str):
        return direct

    # Some payloads nest the entire "message" object Baileys-style.
    msg = payload.get("message") or payload.get("msgContent") or payload.get("data", {})
    if isinstance(msg, dict):
        for key in ("conversation", "text", "body"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                return value
        ext = msg.get("extendedTextMessage")
        if isinstance(ext, dict) and isinstance(ext.get("text"), str):
            return ext["text"]

    return None


def _extract_phone(payload: dict[str, Any]) -> str | None:
    # Try keys that carry a **string** phone/JID. The bare "sender" key is
    # intentionally NOT listed — W-API sends it as a dict and picking that
    # up would short-circuit the subsequent dotted lookups.
    for key in (
        "phone",
        "chat.id",
        "sender.id",
        "from",
        "key.remoteJid",
        "data.phone",
        "data.chat.id",
        "data.sender.id",
        "data.from",
        "data.key.remoteJid",
    ):
        value = _first(payload, key)
        if isinstance(value, str) and value:
            break
    else:
        return None
    # Drop suffixes like "@s.whatsapp.net", "@g.us", "@lid".
    local = value.split("@", 1)[0]
    digits = _DIGITS_RE.sub("", local)
    return digits or None


def _extract_flag(payload: dict[str, Any], *keys: str) -> bool:
    value = _first(payload, *keys)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return False


def _extract_group(payload: dict[str, Any]) -> bool:
    if _extract_flag(payload, "isGroup", "data.isGroup"):
        return True
    remote = _first(
        payload, "key.remoteJid", "data.key.remoteJid", "sender.id", "from"
    )
    return isinstance(remote, str) and "@g.us" in remote


def parse_inbound(payload: dict[str, Any]) -> InboundMessage | None:
    """Parse a W-API webhook payload into an :class:`InboundMessage`.

    Returns ``None`` if the event does not represent a consumable inbound text
    message (non-text media, status updates, typing indicators, etc.).
    """
    text = _extract_text(payload)
    if not text:
        return None

    phone = _extract_phone(payload)
    if not phone:
        return None

    message_id = _first(
        payload,
        "messageId",
        "id",
        "message.id",
        "key.id",
        "data.messageId",
        "data.key.id",
    )
    if not isinstance(message_id, str) or not message_id:
        return None

    sender_name = _first(
        payload,
        "senderName",
        "pushName",
        "sender.pushName",
        "sender.name",
        "data.senderName",
        "data.pushName",
        "data.sender.pushName",
    )

    instance_id = _first(payload, "instanceId", "data.instanceId")

    return InboundMessage(
        message_id=message_id,
        phone=phone,
        text=text.strip(),
        from_me=_extract_flag(payload, "fromMe", "key.fromMe", "data.fromMe", "data.key.fromMe"),
        is_group=_extract_group(payload),
        sender_name=sender_name if isinstance(sender_name, str) else None,
        instance_id=instance_id if isinstance(instance_id, str) else None,
        raw=payload,
    )
