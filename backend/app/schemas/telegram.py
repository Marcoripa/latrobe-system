from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TelegramUser(BaseModel):
    """Telegram user metadata from an incoming webhook update."""

    model_config = ConfigDict(extra="ignore")

    id: int
    is_bot: bool = False
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None


class TelegramChat(BaseModel):
    """Telegram chat metadata containing the chat ID."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(..., description="Unique Telegram chat identifier")
    type: str = Field(default="private", description="Chat type: private, group, supergroup, channel")
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TelegramMessage(BaseModel):
    """Telegram message object extracted from an incoming Update payload."""

    model_config = ConfigDict( populate_by_name=True, extra="ignore")

    message_id: int | None = None
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    date: int | None = None
    text: str | None = Field(default=None, description="UTF-8 text of the incoming message")


class TelegramUpdate(BaseModel):
    """Top-level Telegram Webhook Update payload."""

    model_config = ConfigDict(extra="ignore")

    update_id: int | None = None
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None

    @property
    def effective_message(self) -> TelegramMessage | None:
        """Return either `message` or `edited_message` if present."""
        return self.message or self.edited_message


class ParsedBotIntent(BaseModel):
    """Structured intent extracted from an operator's natural language message."""

    intent: Literal[
        "check_material_stock",
        "check_project_status",
        "list_active_projects",
        "help",
        "unknown",
        "check_ute_stock",
    ] = Field(
        ...,
        description="Classified operational intent of the user's message",
    )
    material_query: str | None = Field(
        default=None,
        description="Extracted material name, SKU, or search term (e.g., '25mm poly pipe', 'solenoid valve')",
    )
    project_identifier: str | None = Field(
        default=None,
        description="Extracted project name, site name, or project ID (e.g., 'Red Hill', 'proj_101')",
    )
    ute_identifier: str | None = Field(
        default=None,
        description="Deprecated ute identifier preserved for backwards compatibility",
    )
