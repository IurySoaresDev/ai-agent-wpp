"""Per-conversation runtime state — human takeover / mute tracking.

Stored in its own small SQLite file so it's orthogonal to the LangGraph
checkpointer DB (resetting memory must not reset mute state, and vice-versa).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import aiosqlite

_MUTED_FOREVER: Final[float] = 0.0  # muted_until == 0 means "until explicit resume".

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_state (
    phone        TEXT PRIMARY KEY,
    muted_until  REAL,
    updated_at   REAL NOT NULL
);
"""


class ConversationStateStore:
    """Tracks which conversations are currently handed off to the human."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def setup(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def is_muted(self, phone: str) -> bool:
        db = self._require_db()
        async with db.execute(
            "SELECT muted_until FROM conversation_state WHERE phone = ?",
            (phone,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return False
        muted_until: float = float(row[0])
        if muted_until == _MUTED_FOREVER:
            return True
        return muted_until > time.time()

    async def mute(self, phone: str, *, duration_seconds: float | None) -> None:
        """Mute a conversation.

        ``duration_seconds=None`` → indefinite mute (requires explicit resume).
        ``duration_seconds=N``    → mute for N seconds from now.
        """
        muted_until = (
            _MUTED_FOREVER if duration_seconds is None else time.time() + max(0.0, duration_seconds)
        )
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO conversation_state (phone, muted_until, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                muted_until = excluded.muted_until,
                updated_at  = excluded.updated_at
            """,
            (phone, muted_until, time.time()),
        )
        await db.commit()

    async def resume(self, phone: str) -> None:
        """Immediately clear any mute for the given conversation."""
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO conversation_state (phone, muted_until, updated_at)
            VALUES (?, NULL, ?)
            ON CONFLICT(phone) DO UPDATE SET
                muted_until = NULL,
                updated_at  = excluded.updated_at
            """,
            (phone, time.time()),
        )
        await db.commit()

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ConversationStateStore.setup() was not called.")
        return self._db
