import logging
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Path, Query, status
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.booking import InboundMessage
from app.schemas.telegram import TelegramUpdate
from app.services.ap_invoice_service import ap_invoice_service
from app.services.booking_service import booking_service, process_booking_request
from app.services.email_service import email_service
from app.services.telegram_service import telegram_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


@router.post(
    "/webhooks/test-inbound",
    status_code=status.HTTP_200_OK,
    summary="Test inbound customer booking request webhook (SMS / Email / Mock adapter)",
)
async def test_inbound_webhook(
    msg: InboundMessage,
) -> dict[str, Any]:
    """Receive an InboundMessage payload (e.g., from SMS, Email, or test harness) and run
    the Human-in-the-Loop booking orchestrator (`process_booking_request`).
    """
    return await process_booking_request(msg)


@router.post(
    "/webhooks/poll-email",
    status_code=status.HTTP_200_OK,
    summary="Poll Gmail IMAP inbox for unread emails and process booking requests",
)
async def poll_email_webhook(
    secret: str | None = Query(
        default=None,
        description="Optional webhook secret for Cloud Scheduler authentication",
    ),
    max_messages: int = Query(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of unread emails to process per run",
    ),
) -> dict[str, Any]:
    """Trigger a Gmail IMAP poll for unread messages, deduplicating via Firestore `processed_emails`
    and passing new messages through `process_booking_request`.
    """
    expected_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if expected_secret and secret is not None and not secrets.compare_digest(secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook secret.",
        )
    return await email_service.poll_and_process_inbox(max_messages=max_messages)


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
    2. Intercepts `callback_query` events from Inline Keyboard buttons and dispatches `booking_service.handle_callback_query`.
    3. Extracts `message.chat.id` and `message.text` safely and dispatches `telegram_service.process_incoming_message`.
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

    # 1. Check for callback_query (Inline Keyboard button presses)
    raw_cb = payload.get("callback_query")
    if isinstance(raw_cb, dict) and raw_cb.get("id") and raw_cb.get("data"):
        cb_id = str(raw_cb["id"])
        cb_data = str(raw_cb["data"])
        cb_msg = raw_cb.get("message") or {}
        cb_chat = cb_msg.get("chat") or {}
        cb_from = raw_cb.get("from") or {}
        cb_chat_id = int(cb_chat["id"]) if cb_chat.get("id") is not None else None
        cb_message_id = int(cb_msg["message_id"]) if cb_msg.get("message_id") is not None else None
        cb_user_id = int(cb_from["id"]) if cb_from.get("id") is not None else None

        if cb_data.startswith(("approve_bill|", "reject_bill|")):
            await ap_invoice_service.handle_bill_callback_query(
                cb_id,
                cb_data,
                cb_chat_id,
                cb_message_id,
                cb_user_id,
            )
        else:
            await booking_service.handle_callback_query(
                cb_id,
                cb_data,
                cb_chat_id,
                cb_message_id,
                cb_user_id,
            )
        return {"status": "ok", "queued": False, "processed": True, "type": "callback_query"}

    # 2. Extract standard message, chat.id, user_id, and text from the dict payload
    chat_id: int | None = None
    user_id: int | None = None
    text: str | None = None

    try:
        update = TelegramUpdate.model_validate(payload)
        if update.callback_query and update.callback_query.data:
            cb = update.callback_query
            cb_data_str = str(cb.data)
            if cb_data_str.startswith(("approve_bill|", "reject_bill|")):
                await ap_invoice_service.handle_bill_callback_query(
                    cb.id,
                    cb_data_str,
                    cb.message.chat.id if cb.message else None,
                    cb.message.message_id if cb.message else None,
                    cb.from_user.id if cb.from_user else None,
                )
            else:
                await booking_service.handle_callback_query(
                    cb.id,
                    cb_data_str,
                    cb.message.chat.id if cb.message else None,
                    cb.message.message_id if cb.message else None,
                    cb.from_user.id if cb.from_user else None,
                )
            return {"status": "ok", "queued": False, "processed": True, "type": "callback_query"}

        msg = update.effective_message
        if msg and msg.text:
            chat_id = msg.chat.id
            text = msg.text.strip()
            if msg.from_user:
                user_id = msg.from_user.id
    except ValidationError:
        # Fallback direct dict extraction if Telegram sends an unconventional payload shape
        raw_message = payload.get("message") or payload.get("edited_message")
        if isinstance(raw_message, dict):
            raw_chat = raw_message.get("chat")
            raw_text = raw_message.get("text")
            raw_from = raw_message.get("from")
            if isinstance(raw_chat, dict) and raw_chat.get("id") is not None and isinstance(raw_text, str):
                chat_id = int(raw_chat["id"])
                text = raw_text.strip()
                if isinstance(raw_from, dict) and raw_from.get("id") is not None:
                    user_id = int(raw_from["id"])

    if chat_id is not None and text:
        await telegram_service.process_incoming_message(
            chat_id,
            text,
            user_id,
        )
        return {"status": "ok", "queued": False, "processed": True, "type": "message"}

    return {"status": "ok", "queued": False, "reason": "no_text_message"}
