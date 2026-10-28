import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.telegram import ParsedBotIntent
from app.services.firestore_service import (
    FirestoreService,
    FirestoreServiceError,
    firestore_service,
)
from app.services.llm_service import LLMService, llm_service

logger = logging.getLogger(__name__)


class TelegramNotificationError(Exception):
    """Raised when sending a Telegram notification or configuring a webhook fails."""


class TelegramService:
    """Asynchronous Telegram Bot API client for outbound alerts and inbound natural language queries."""

    BASE_URL = "https://api.telegram.org"
    HISTORY_TTL_SECONDS = 900  # 15 minutes
    MAX_HISTORY_TURNS = 6  # Last 6 messages (3 user + 3 model turns)

    def __init__(
        self,
        bot_token: str | None = None,
        admin_chat_id: str | None = None,
        fs_service: FirestoreService | None = None,
        llm: LLMService | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._bot_token = (
            bot_token if bot_token is not None else settings.TELEGRAM_BOT_TOKEN
        )
        self._admin_chat_id = (
            admin_chat_id
            if admin_chat_id is not None
            else settings.TELEGRAM_ADMIN_CHAT_ID
        )
        self._firestore = fs_service or firestore_service
        self._llm = llm or llm_service
        self._timeout = timeout
        self._chat_history: dict[int, list[tuple[float, dict[str, Any]]]] = {}
        self._http_client: httpx.AsyncClient | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Return a persistent httpx.AsyncClient for connection pooling."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._http_client

    @property
    def is_configured(self) -> bool:
        """Check whether Telegram Bot credentials are configured."""
        return bool(self._bot_token and self._admin_chat_id)

    def _build_qbo_invoice_url(self, qbo_invoice_id: str) -> str:
        """Construct the QuickBooks Online deep link for reviewing the draft invoice."""
        if settings.QUICKBOOKS_ENVIRONMENT == "production":
            return f"https://app.qbo.intuit.com/app/invoice?txnId={qbo_invoice_id}"
        return f"https://app.sandbox.qbo.intuit.com/app/invoice?txnId={qbo_invoice_id}"

    async def set_webhook(
        self,
        base_https_url: str | None = None,
        secret_token: str | None = None,
    ) -> dict[str, Any]:
        """Register the Telegram webhook URL by calling
        `https://api.telegram.org/bot{TOKEN}/setWebhook?url={YOUR_HTTPS_URL}/webhook/telegram/{SECRET_TOKEN}`.
        """
        token = self._bot_token or settings.TELEGRAM_BOT_TOKEN
        resolved_base = (base_https_url or settings.TELEGRAM_WEBHOOK_BASE_URL).rstrip("/")
        resolved_secret = secret_token or settings.TELEGRAM_WEBHOOK_SECRET

        if not token or not resolved_base or not resolved_secret:
            logger.info(
                "Telegram webhook registration skipped (TELEGRAM_BOT_TOKEN, "
                "TELEGRAM_WEBHOOK_BASE_URL, or TELEGRAM_WEBHOOK_SECRET not set)."
            )
            return {"ok": False, "skipped": True, "reason": "webhook_settings_incomplete"}

        webhook_target_url = f"{resolved_base}/webhook/telegram/{resolved_secret}"
        api_url = f"{self.BASE_URL}/bot{token}/setWebhook"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    api_url,
                    params={"url": webhook_target_url},
                    json={
                        "url": webhook_target_url,
                        "allowed_updates": ["message", "edited_message", "callback_query"],
                        "drop_pending_updates": False,
                    },
                )
        except httpx.RequestError as exc:
            logger.error("Network error registering Telegram webhook: %s", exc)
            raise TelegramNotificationError(
                f"Failed to register Telegram webhook: {exc}"
            ) from exc

        if response.status_code != 200:
            raise TelegramNotificationError(
                f"Telegram setWebhook returned HTTP {response.status_code}: {response.text}"
            )

        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise TelegramNotificationError(f"Telegram setWebhook error: {data}")

        logger.info("Successfully registered Telegram webhook at %s/webhook/telegram/***", resolved_base)
        return data

    async def send_message(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]:
        """Send an asynchronous message via the Telegram Bot API `sendMessage` endpoint."""
        target_chat_id = str(chat_id) if chat_id is not None else self._admin_chat_id
        token = self._bot_token or settings.TELEGRAM_BOT_TOKEN
        if not token or not target_chat_id:
            logger.warning(
                "Telegram credentials not configured; skipping Telegram notification."
            )
            return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}

        url = f"{self.BASE_URL}/bot{token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": target_chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            client = self._get_http_client()
            response = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error sending Telegram message: %s", exc)
            raise TelegramNotificationError(
                f"Network error communicating with Telegram API: {exc}"
            ) from exc

        if response.status_code != 200:
            body = response.text
            logger.error(
                "Telegram API returned HTTP %d: %s", response.status_code, body
            )
            raise TelegramNotificationError(
                f"Telegram API error (HTTP {response.status_code}): {body}"
            )

        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            logger.error("Telegram API responded with ok=False: %s", data)
            raise TelegramNotificationError(f"Telegram API error: {data}")

        return data

    async def send_chat_action(
        self,
        *,
        chat_id: str | int | None = None,
        action: str = "typing",
    ) -> dict[str, Any]:
        """Send a chat action (e.g., 'typing') via the Telegram Bot API `sendChatAction` endpoint."""
        target_chat_id = str(chat_id) if chat_id is not None else self._admin_chat_id
        token = self._bot_token or settings.TELEGRAM_BOT_TOKEN
        if not token or not target_chat_id:
            return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}

        url = f"{self.BASE_URL}/bot{token}/sendChatAction"
        payload: dict[str, Any] = {
            "chat_id": target_chat_id,
            "action": action,
        }

        try:
            client = self._get_http_client()
            response = await client.post(url, json=payload, timeout=5.0)
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.debug("Non-critical error sending Telegram chat action: %s", exc)
        return {"ok": False}

    async def edit_message(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        text: str,
        parse_mode: str | None = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Edit an existing message in place via the Telegram Bot API `editMessageText` endpoint."""
        token = self._bot_token or settings.TELEGRAM_BOT_TOKEN
        if not token:
            return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}

        url = f"{self.BASE_URL}/bot{token}/editMessageText"
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            client = self._get_http_client()
            response = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error editing Telegram message: %s", exc)
            raise TelegramNotificationError(
                f"Network error communicating with Telegram API: {exc}"
            ) from exc

        if response.status_code != 200:
            body = response.text
            logger.error(
                "Telegram editMessageText returned HTTP %d: %s", response.status_code, body
            )
            raise TelegramNotificationError(
                f"Telegram API error (HTTP {response.status_code}): {body}"
            )

        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise TelegramNotificationError(f"Telegram API error: {data}")

        return data

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Instantly update or remove inline keyboard buttons via Telegram's `editMessageReplyMarkup` endpoint."""
        token = self._bot_token or settings.TELEGRAM_BOT_TOKEN
        if not token:
            return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}

        url = f"{self.BASE_URL}/bot{token}/editMessageReplyMarkup"
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []},
        }

        try:
            client = self._get_http_client()
            response = await client.post(url, json=payload, timeout=5.0)
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.debug("Non-critical error editing message reply markup: %s", exc)
        return {"ok": False}

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        """Acknowledge a callback_query button press via Telegram's `answerCallbackQuery` endpoint."""
        token = self._bot_token or settings.TELEGRAM_BOT_TOKEN
        if not token or not callback_query_id:
            return {"ok": False, "skipped": True}

        url = f"{self.BASE_URL}/bot{token}/answerCallbackQuery"
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]

        try:
            client = self._get_http_client()
            response = await client.post(url, json=payload, timeout=5.0)
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.debug("Non-critical error answering callback query: %s", exc)
        return {"ok": False}

    @staticmethod
    def build_booking_approval_keyboard(proposal_id: str) -> dict[str, Any]:
        """Construct the 3-option Human-in-the-Loop Inline Keyboard for booking proposals."""
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Confirm & Notify",
                        "callback_data": f"confirm_notify|{proposal_id}",
                    },
                    {
                        "text": "🤫 Confirm (Silent)",
                        "callback_data": f"confirm_silent|{proposal_id}",
                    },
                ],
                [
                    {
                        "text": "❌ Reject",
                        "callback_data": f"reject|{proposal_id}",
                    }
                ],
            ]
        }

    @staticmethod
    def format_proposed_slot(start_iso: str, end_iso: str) -> str:
        """Format ISO 8601 start/end timestamps into a readable string, e.g. 'Wed, 28 Oct 2026 at 09:00 to 11:00'."""
        try:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            date_str = start_dt.strftime("%a, %d %b %Y")
            start_time = start_dt.strftime("%H:%M")
            end_time = end_dt.strftime("%H:%M")
            if start_dt.date() == end_dt.date():
                return f"{date_str} at {start_time} to {end_time}"
            end_date_str = end_dt.strftime("%a, %d %b %Y")
            return f"{date_str} at {start_time} to {end_date_str} at {end_time}"
        except Exception:
            return f"{start_iso} to {end_iso}"

    def format_booking_proposal_text(
        self,
        proposal: dict[str, Any],
        final_status: str | None = None,
    ) -> str:
        """Render the HTML text for a Telegram booking approval card."""
        client_flag = str(proposal.get("customer_flag") or "MATCHED CLIENT")
        customer_name = html.escape(str(proposal.get("customer_name") or "NEW/UNKNOWN CLIENT"))
        customer_id = html.escape(str(proposal.get("qbo_customer_id") or "N/A"))
        overdue = float(proposal.get("overdue_balance") or 0.0)
        source = html.escape(str(proposal.get("source") or "unknown").upper())
        sender_id = html.escape(str(proposal.get("sender_id") or "unknown"))
        project_name = html.escape(str(proposal.get("project_name") or "Unassigned"))
        project_id = html.escape(str(proposal.get("project_id") or "unassigned"))
        urgency = html.escape(str(proposal.get("urgency") or "normal").upper())
        summary = html.escape(str(proposal.get("summary") or "Service Booking"))
        job_desc = html.escape(str(proposal.get("job_description") or ""))
        raw_start = str(proposal.get("start_iso") or "")
        raw_end = str(proposal.get("end_iso") or "")
        readable_slot = html.escape(self.format_proposed_slot(raw_start, raw_end))
        location = html.escape(str(proposal.get("location") or "Not specified"))
        raw_text = html.escape(str(proposal.get("raw_text") or ""))

        if client_flag == "NEW/UNKNOWN CLIENT":
            identity_line = f"⚠️ <b>Client:</b> <code>NEW/UNKNOWN CLIENT</code>"
        else:
            balance_badge = (
                f" (⚠️ <b>Overdue/Balance: ${overdue:,.2f}</b>)"
                if overdue > 0
                else " (Balance: $0.00)"
            )
            identity_line = (
                f"<b>Client:</b> {customer_name} (QBO ID: <code>{customer_id}</code>){balance_badge}"
            )

        lines = [
            "📅 <b>New Booking Proposal</b>",
            "",
            identity_line,
            f"<b>Channel:</b> {source} (<code>{sender_id}</code>)",
            f"<b>Project:</b> {project_name} (<code>{project_id}</code>)",
            f"<b>Urgency:</b> {urgency}",
            f"<b>Summary:</b> {summary}",
            # f"<b>Job Description:</b> {job_desc}",
            f"<b>Location:</b> {location}",
            f"<b>Proposed Slot:</b> <code>{readable_slot}</code>",
            "",
            f"💬 <i>\"{raw_text}\"</i>",
        ]

        if final_status:
            lines.extend(["", f"<b>Status:</b> {final_status}"])

        return "\n".join(lines)

    async def send_booking_proposal_card(
        self,
        proposal_id: str,
        proposal: dict[str, Any],
        chat_id: str | int | None = None,
    ) -> dict[str, Any]:
        """Format and send an interactive booking proposal card with Confirm/Silent/Reject buttons."""
        message_text = self.format_booking_proposal_text(proposal)
        reply_markup = self.build_booking_approval_keyboard(proposal_id)
        return await self.send_message(
            text=message_text,
            chat_id=chat_id,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    def _get_recent_history(self, chat_id: int) -> list[dict[str, Any]]:
        """Return non-expired conversation turns for the given chat_id."""
        now = time.time()
        entries = self._chat_history.get(chat_id, [])
        valid = [
            (ts, turn)
            for ts, turn in entries
            if now - ts <= self.HISTORY_TTL_SECONDS
        ][-self.MAX_HISTORY_TURNS :]
        self._chat_history[chat_id] = valid
        return [turn for _, turn in valid]

    def _append_history(self, chat_id: int, user_text: str, model_text: str) -> None:
        """Append the latest user prompt and model response to the rolling chat history."""
        now = time.time()
        entries = self._chat_history.get(chat_id, [])
        entries.append((now, {"role": "user", "parts": [{"text": user_text}]}))
        entries.append((now, {"role": "model", "parts": [{"text": model_text}]}))
        self._chat_history[chat_id] = entries[-self.MAX_HISTORY_TURNS :]

    async def _handle_conversational_llm(
        self, text: str, chat_id: int | None = None
    ) -> str | None:
        """Handle open-ended conversational questions using the autonomous ReAct agent loop.
        Returns None if an error occurs, allowing graceful fallback to standard guidance.
        """
        cleaned_text = text.strip()
        if not cleaned_text:
            return None

        history = self._get_recent_history(chat_id) if chat_id is not None else None

        try:
            response = await self._llm.run_agent(cleaned_text, history=history)
            if response and chat_id is not None:
                self._append_history(chat_id, cleaned_text, response)
            return response
        except Exception as exc:
            logger.exception("Error executing ReAct agent for Telegram message: %s", exc)
            return None

    async def _evaluate_text(self, text: str) -> ParsedBotIntent:
        """Evaluate incoming operator text using heuristic fast-path first."""
        cleaned_text = text.strip()
        if not cleaned_text:
            return ParsedBotIntent(intent="help")

        return self._heuristic_parse_intent(cleaned_text)

    async def _evaluate_text_with_llm(self, text: str) -> ParsedBotIntent:
        """Backwards-compatible wrapper returning ParsedBotIntent."""
        return await self._evaluate_text(text)

    @staticmethod
    def _heuristic_parse_intent(text: str) -> ParsedBotIntent:
        """Fast-path parser for explicit operator keywords and command patterns."""
        lowered = text.lower().strip()

        # Exact keywords
        if lowered in ("/start", "/help", "help", "commands", "?"):
            return ParsedBotIntent(intent="help")

        if lowered in ("projects", "active projects", "list projects", "/projects"):
            return ParsedBotIntent(intent="list_active_projects")

        # Explicit slash commands
        if lowered.startswith("/status "):
            proj_id = text[8:].strip()
            if proj_id:
                return ParsedBotIntent(intent="check_project_status", project_identifier=proj_id)

        if lowered.startswith(("/material ", "/stock ")):
            mat_query = text.split(maxsplit=1)[1].strip()
            if mat_query:
                return ParsedBotIntent(intent="check_material_stock", material_query=mat_query)

        # Strict command prefixes (e.g. "status of Red Hill", "stock of 25mm pipe")
        status_match = re.search(
            r"^(?:status|summary)\s+(?:of|for|on)?\s*(?:the\s+)?(.+?)(?:\s+project|\s+job)?$",
            text,
            flags=re.IGNORECASE,
        )
        if status_match:
            extracted = status_match.group(1).strip(" ?!.")
            if extracted and extracted.lower() not in ("project", "job"):
                return ParsedBotIntent(
                    intent="check_project_status",
                    project_identifier=extracted,
                )

        stock_match = re.search(
            r"^(?:stock|inventory|material|parts)\s+(?:of|for|on)?\s*(.+?)$",
            text,
            flags=re.IGNORECASE,
        )
        if stock_match:
            extracted = stock_match.group(1).strip(" ?.!")
            if extracted:
                return ParsedBotIntent(
                    intent="check_material_stock",
                    material_query=extracted,
                )

        return ParsedBotIntent(intent="unknown")

    async def _build_response_for_intent(self, parsed: ParsedBotIntent) -> str:
        """Query Firestore based on the parsed intent and format a human-readable response."""
        if parsed.intent == "check_material_stock" and parsed.material_query:
            materials = await self._firestore.search_materials(
                query=parsed.material_query, limit=10
            )
            escaped_query = html.escape(parsed.material_query)
            if not materials:
                return f"No materials found matching <b>{escaped_query}</b> in master inventory."

            lines = [f"📦 <b>Master Inventory matching '{escaped_query}':</b>"]
            for m in materials:
                name = html.escape(str(m.get("name") or m.get("sku") or "Item"))
                sku = html.escape(str(m.get("sku") or ""))
                unit = html.escape(str(m.get("unit") or "Units"))
                price = float(m.get("unit_price") or 0.0)
                lines.append(f"• <b>{name}</b> (<code>{sku}</code>) — ${price:.2f}/{unit}")
            return "\n".join(lines)

        if parsed.intent == "check_project_status" and parsed.project_identifier:
            status_data = await self._firestore.get_project_status(
                parsed.project_identifier
            )
            if not status_data.get("found"):
                escaped_query = html.escape(parsed.project_identifier)
                return f"Could not find a project matching <b>{escaped_query}</b>."

            project_id = html.escape(str(status_data.get("project_id") or ""))
            project_name = html.escape(str(status_data.get("name") or project_id))
            client_name = html.escape(str(status_data.get("client_name") or "N/A"))
            status_val = html.escape(str(status_data.get("status") or "active"))
            location = status_data.get("location")
            total_hours = float(status_data.get("total_hours") or 0.0)
            shifts_count = int(status_data.get("shifts_count") or 0)
            unbilled_hours = float(status_data.get("unbilled_hours") or 0.0)
            unbilled_shifts_count = int(status_data.get("unbilled_shifts_count") or 0)
            items_used = status_data.get("items_used") or []

            lines = [
                f"📋 <b>Project Status: {project_name}</b> (<code>{project_id}</code>)",
                f"• <b>Client:</b> {client_name}",
                f"• <b>Status:</b> {status_val}",
            ]
            if location:
                lines.append(f"• <b>Location:</b> {html.escape(str(location))}")
            lines.append(
                f"• <b>Total Hours Logged:</b> {total_hours:g} hrs ({shifts_count} shifts)"
            )
            lines.append(
                f"• <b>Unbilled Hours:</b> {unbilled_hours:g} hrs ({unbilled_shifts_count} unbilled shifts)"
            )
            if items_used:
                lines.append("\n🧰 <b>Materials Used:</b>")
                for item in items_used:
                    i_name = html.escape(str(item.get("name") or item.get("sku") or "Item"))
                    i_sku = html.escape(str(item.get("sku") or ""))
                    i_qty = float(item.get("total_quantity") or 0.0)
                    lines.append(f"  • {i_name} (<code>{i_sku}</code>): <b>{i_qty:g}</b>")

            return "\n".join(lines)

        if parsed.intent == "list_active_projects":
            projects = await self._firestore.list_projects(
                status_filter="active", limit=10
            )
            if not projects:
                return "There are currently no active projects in Firestore."
            lines = ["🏗 <b>Active Projects:</b>"]
            for proj in projects:
                p_name = html.escape(str(proj.get("name") or proj.get("id")))
                p_client = html.escape(str(proj.get("client_name") or "Unknown Client"))
                lines.append(f"• <b>{p_name}</b> ({p_client})")
            return "\n".join(lines)

        return (
            "👋 <b>LaTrobe Irrigation Operations Bot</b>\n\n"
            "Try asking me:\n"
            "• <i>\"projects\"</i> — List active projects\n"
            "• <i>\"status of Red Hill project\"</i> — Check hours & status\n"
            "• <i>\"stock of 25mm pipe\"</i> — Search master inventory\n"
            "• Or ask an open-ended question about projects or inventory!"
        )

    async def process_incoming_message(
        self,
        chat_id: int,
        text: str,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Process an incoming operator message from the Telegram webhook:
        0. Authorization: Verifies user_id against TELEGRAM_USER_IDS.
        1. Fast-path: Evaluates simple commands and explicit keywords deterministically (<1ms).
        2. Escalation: If conversational / unknown, uses LLM Tool Calling to query Firestore.
        3. Formats the retrieved data into a clean, readable string.
        4. Calls `send_message` to return the answer to the user.
        """
        # 0. Telegram user authorization check
        effective_user_id = user_id if user_id is not None else chat_id
        allowed_users = settings.TELEGRAM_USER_IDS
        if allowed_users and effective_user_id not in allowed_users:
            logger.warning(
                "Unauthorized access attempt to Telegram bot from user_id=%s (chat_id=%s)",
                effective_user_id,
                chat_id,
            )
            try:
                return await self.send_message(
                    chat_id=chat_id,
                    text="⛔ <b>Access Denied:</b> You are not authorized to use this operational bot.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.error("Failed to send unauthorized notice to chat_id=%s: %s", chat_id, exc)
                return {"ok": False, "reason": "unauthorized"}

        ack_message_id: int | None = None

        try:
            # 1. Fast-path heuristic check for simple keywords & slash commands
            heuristic_intent = self._heuristic_parse_intent(text)

            if heuristic_intent.intent != "unknown":
                logger.info(
                    "Heuristic fast-path matched intent '%s' (project=%s, material=%s)",
                    heuristic_intent.intent,
                    heuristic_intent.project_identifier,
                    heuristic_intent.material_query,
                )
                reply_text = await self._build_response_for_intent(heuristic_intent)
            else:
                # 2. Conversational open-ended query -> Concurrent Acknowledgment + Typing Indicator + ReAct Agent
                logger.info("Conversational query; invoking Autonomous ReAct Agent: %r", text)

                async def _send_quick_ack() -> int | None:
                    try:
                        ack_response, _ = await asyncio.gather(
                            self.send_message(
                                chat_id=chat_id,
                                text="⏳ <i>I'm on it! Checking the records...</i>",
                                parse_mode="HTML",
                            ),
                            self.send_chat_action(chat_id=chat_id, action="typing"),
                            return_exceptions=True,
                        )
                        if isinstance(ack_response, dict):
                            return ack_response.get("result", {}).get("message_id")
                    except Exception as ack_exc:
                        logger.debug("Non-critical failure sending quick acknowledgment: %s", ack_exc)
                    return None

                ack_message_id, llm_response = await asyncio.gather(
                    _send_quick_ack(),
                    self._handle_conversational_llm(text, chat_id=chat_id),
                )
                if llm_response:
                    reply_text = llm_response
                else:
                    # Fallback to standard guidance if LLM is unavailable or failed
                    reply_text = await self._build_response_for_intent(heuristic_intent)
        except FirestoreServiceError as exc:
            logger.error(
                "Firestore error while processing Telegram message for chat_id=%s: %s",
                chat_id,
                exc,
                )
            reply_text = (
                "⚠️ Sorry, I encountered a database error while looking up that information. "
                "Please try again shortly."
            )
        except Exception as exc:
            logger.exception(
                "Unexpected error processing Telegram message from chat_id=%s: %s",
                chat_id,
                exc,
            )
            reply_text = (
                "⚠️ Sorry, something went wrong while processing your request."
            )

        if ack_message_id is not None:
            try:
                return await self.edit_message(
                    chat_id=chat_id,
                    message_id=ack_message_id,
                    text=reply_text,
                    parse_mode="HTML",
                )
            except Exception as edit_exc:
                logger.warning(
                    "Failed to edit acknowledgment message %s in chat %s (%s); falling back to send_message.",
                    ack_message_id,
                    chat_id,
                    edit_exc,
                )

        try:
            return await self.send_message(
                text=reply_text,
                chat_id=chat_id,
                parse_mode="HTML",
            )
        except TelegramNotificationError as exc:
            logger.error(
                "Failed to send Telegram reply to chat_id=%s: %s", chat_id, exc
            )
            return {"ok": False, "error": str(exc)}

    async def notify_manager_draft_invoice(
        self,
        project_name: str,
        total_amount: float,
        qbo_invoice_id: str,
    ) -> dict[str, Any]:
        """Format and send a Telegram alert with an inline review button for a newly created draft invoice."""
        escaped_project = html.escape(project_name)
        escaped_invoice_id = html.escape(str(qbo_invoice_id))
        review_url = self._build_qbo_invoice_url(str(qbo_invoice_id))

        message_text = (
            "🧾 <b>New Draft Invoice Ready for Review</b>\n\n"
            f"<b>Project:</b> {escaped_project}\n"
            f"<b>Total Amount:</b> ${total_amount:,.2f} AUD\n"
            f"<b>QuickBooks Invoice ID:</b> <code>{escaped_invoice_id}</code>\n"
            "<b>Status:</b> DRAFT (<i>Not emailed to client</i>)\n\n"
            f'🔗 <a href="{review_url}">Open Draft Invoice in QuickBooks Online</a>'
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔍 Review Draft Invoice in QuickBooks",
                        "url": review_url,
                    }
                ]
            ]
        }

        result = await self.send_message(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        logger.info(
            "Sent Telegram draft invoice notification for project '%s' (Invoice ID: %s)",
            project_name,
            qbo_invoice_id,
        )
        return result

    async def notify_low_stock_alerts(
        self, ute_id: str, alerts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Format and send a Telegram alert when stock items fall at or below reorder threshold."""
        if not alerts:
            return {"ok": True, "skipped": True, "reason": "no_low_stock_items"}

        escaped_loc = html.escape(ute_id)
        lines = [
            f"⚠️ <b>Low Stock Alert ({escaped_loc})</b>\n",
            "The following master stock items are at or below their reorder threshold:\n",
        ]
        for item in alerts:
            name = html.escape(str(item.get("name", "Unknown Item")))
            sku = html.escape(str(item.get("sku", "N/A")))
            qty = float(item.get("current_quantity", item.get("quantity", 0.0)))
            threshold = float(item.get("reorder_threshold", 0.0))
            lines.append(
                f"• <b>{name}</b> (<code>{sku}</code>): <b>{qty:g}</b> remaining "
                f"<i>(threshold: {threshold:g})</i>"
            )

        message_text = "\n".join(lines)
        result = await self.send_message(text=message_text, parse_mode="HTML")
        logger.info(
            "Sent Telegram low-stock alert for '%s' (%d item(s))",
            ute_id,
            len(alerts),
        )
        return result


telegram_service = TelegramService()
