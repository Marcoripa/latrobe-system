import logging
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Path, status
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.telegram import TelegramUpdate
from app.services.telegram_service import telegram_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


@router.post(
    "/webhook/telegram/{secret_token}",
    status_code=status.HTTP_200_OK,
    summary="Receive incoming Telegram Bot webhook updates",
)
async def telegram_webhook(
    background_tasks: BackgroundTasks,
    secret_token: str = Path(
        ..., description="Secret verification token configured for the Telegram webhook"
    ),
    payload: dict[str, Any] = Body(
        ..., description="Raw Telegram Update JSON payload"
    ),
) -> dict[str, Any]:
    """Handle incoming Telegram Bot webhook POST requests:
    1. Verifies `secret_token` against `TELEGRAM_WEBHOOK_SECRET` (raises HTTP 403 on mismatch).
    2. Extracts `message.chat.id` and `message.text` safely.
    3. Dispatches `telegram_service.process_incoming_message(chat_id, text)` via `BackgroundTasks`
       so the endpoint immediately responds with `200 OK` without blocking Telegram's webhook delivery.
    """
    expected_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected_secret or not secrets.compare_digest(
        secret_token, expected_secret
    ):
        logger.warning("Rejected unauthorized Telegram webhook request with invalid secret_token.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Telegram webhook secret token.",
        )

    # Extract message, chat.id, and text from the dict payload (validated via TelegramUpdate model)
    chat_id: int | None = None
    text: str | None = None

    try:
        update = TelegramUpdate.model_validate(payload)
        msg = update.effective_message
        if msg and msg.text:
            chat_id = msg.chat.id
            text = msg.text.strip()
    except ValidationError:
        # Fallback direct dict extraction if Telegram sends an unconventional payload shape
        raw_message = payload.get("message") or payload.get("edited_message")
        if isinstance(raw_message, dict):
            raw_chat = raw_message.get("chat")
            raw_text = raw_message.get("text")
            if isinstance(raw_chat, dict) and raw_chat.get("id") is not None and isinstance(raw_text, str):
                chat_id = int(raw_chat["id"])
                text = raw_text.strip()

    if chat_id is not None and text:
        background_tasks.add_task(
            telegram_service.process_incoming_message,
            chat_id,
            text,
        )
        return {"status": "ok", "queued": True}

    return {"status": "ok", "queued": False, "reason": "no_text_message"}
