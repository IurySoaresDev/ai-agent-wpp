"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All runtime configuration. Secrets are wrapped in SecretStr."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- W-API ----
    wapi_token: SecretStr
    wapi_instance_id: str
    wapi_base_url: str = "https://api.w-api.app/v1"
    webhook_secret: SecretStr

    # ---- Mistral ----
    mistral_api_key: SecretStr
    mistral_model: str = "mistral-large-latest"
    mistral_temperature: float = 0.3
    mistral_max_tokens: int = 1024

    # ---- Agent ----
    agent_prompt_path: Path = Path("prompts/system.md")
    agent_history_window: int = Field(default=30, ge=2, le=200)
    agent_checkpoint_db: Path = Path("data/checkpoints.sqlite")
    agent_state_db: Path = Path("data/state.sqlite")

    # ---- Human takeover ----
    # When the instance owner sends a WhatsApp message in a conversation,
    # mute the bot for this many seconds before resuming automatically.
    # Set to 0 to disable the auto-mute window entirely.
    takeover_window_seconds: int = Field(default=1800, ge=0, le=86_400)
    takeover_pause_commands: str = "!pausar,!pause,!ai off,!bot off"
    takeover_resume_commands: str = "!retomar,!resume,!ai on,!bot on"

    # ---- Server ----
    host: str = "0.0.0.0"  # noqa: S104 — bind-all is intentional for containers
    port: int = 8000
    log_level: str = "INFO"
    allowed_senders: str = ""

    @field_validator("wapi_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("agent_prompt_path", "agent_checkpoint_db", "agent_state_db")
    @classmethod
    def _resolve_path(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @property
    def allowed_sender_set(self) -> frozenset[str]:
        raw = (self.allowed_senders or "").strip()
        if not raw:
            return frozenset()
        return frozenset(
            part.strip().lstrip("+") for part in raw.split(",") if part.strip()
        )

    @property
    def takeover_pause_command_set(self) -> frozenset[str]:
        return _split_commands(self.takeover_pause_commands)

    @property
    def takeover_resume_command_set(self) -> frozenset[str]:
        return _split_commands(self.takeover_resume_commands)


def _split_commands(raw: str) -> frozenset[str]:
    return frozenset(
        part.strip().lower()
        for part in (raw or "").split(",")
        if part.strip()
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
