import html
import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.telegram import ParsedBotIntent
from app.services.firestore_service import (
    FirestoreService,
    FirestoreServiceError,
    firestore_service,
)

logger = logging.getLogger(__name__)


class TelegramNotificationError(Exception):
    """Raised when sending a Telegram notification or configuring a webhook fails."""


class TelegramService:
    """Asynchronous Telegram Bot API client for outbound alerts and inbound natural language queries."""

    BASE_URL = "https://api.telegram.org"
    GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
    OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

    SYSTEM_PROMPT = (
        "You are the operations parser for LaTrobe Irrigation's Telegram field bot. "
        "Convert the operator's message into a JSON object matching this exact schema:\n"
        "{\n"
        '  "intent": "check_ute_stock" | "check_project_status" | "list_active_projects" | "help" | "unknown",\n'
        '  "ute_identifier": string or null,\n'
        '  "project_identifier": string or null\n'
        "}\n"
        "Examples:\n"
        '- "stock on John\'s ute" -> {"intent": "check_ute_stock", "ute_identifier": "John\'s Ute", "project_identifier": null}\n'
        '- "check inventory for ute-02" -> {"intent": "check_ute_stock", "ute_identifier": "ute-02", "project_identifier": null}\n'
        '- "status of Red Hill project" -> {"intent": "check_project_status", "ute_identifier": null, "project_identifier": "Red Hill"}\n'
        '- "how many hours on proj_101?" -> {"intent": "check_project_status", "ute_identifier": null, "project_identifier": "proj_101"}\n'
        '- "active projects" -> {"intent": "list_active_projects", "ute_identifier": null, "project_identifier": null}\n'
        "Respond ONLY with valid JSON."
    )

    def __init__(
        self,
        bot_token: str | None = None,
        admin_chat_id: str | None = None,
        fs_service: FirestoreService | None = None,
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
        self._timeout = timeout

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
                        "allowed_updates": ["message", "edited_message"],
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
            async with httpx.AsyncClient(timeout=self._timeout) as client:
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

    async def _evaluate_text_with_llm(self, text: str) -> ParsedBotIntent:
        """Evaluate incoming operator text using an LLM (Gemini or OpenAI) with a deterministic
        fallback parser if LLM credentials are not configured or an API error occurs.
        """
        cleaned_text = text.strip()
        if not cleaned_text:
            return ParsedBotIntent(intent="help")

        # 1. Try Google Gemini API if GEMINI_API_KEY is configured
        if settings.GEMINI_API_KEY:
            model_name = settings.LLM_MODEL or "gemini-2.5-flash"
            url = f"{self.GEMINI_API_BASE}/{model_name}:generateContent"
            payload = {
                "system_instruction": {"parts": [{"text": self.SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": cleaned_text}]}],
                "generationConfig": {
                    "temperature": 0.0,
                    "responseMimeType": "application/json",
                },
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        url,
                        params={"key": settings.GEMINI_API_KEY},
                        json=payload,
                    )
                if resp.status_code == 200:
                    resp_data = resp.json()
                    raw_json = (
                        resp_data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )
                    if raw_json:
                        return ParsedBotIntent.model_validate_json(raw_json)
                else:
                    logger.warning(
                        "Gemini API returned HTTP %d; falling back to heuristic parser.",
                        resp.status_code,
                    )
            except Exception as exc:
                logger.warning(
                    "LLM evaluation via Gemini failed (%s); falling back to heuristic parser.",
                    exc,
                )

        # 2. Try OpenAI API if OPENAI_API_KEY is configured
        if settings.OPENAI_API_KEY:
            model_name = (
                settings.LLM_MODEL
                if settings.LLM_MODEL.startswith("gpt-")
                else "gpt-4o-mini"
            )
            payload = {
                "model": model_name,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": cleaned_text},
                ],
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        self.OPENAI_API_URL,
                        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                        json=payload,
                    )
                if resp.status_code == 200:
                    resp_data = resp.json()
                    raw_json = (
                        resp_data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    if raw_json:
                        return ParsedBotIntent.model_validate_json(raw_json)
            except Exception as exc:
                logger.warning(
                    "LLM evaluation via OpenAI failed (%s); falling back to heuristic parser.",
                    exc,
                )

        # 3. Deterministic heuristic parser fallback
        return self._heuristic_parse_intent(cleaned_text)

    @staticmethod
    def _heuristic_parse_intent(text: str) -> ParsedBotIntent:
        """Fallback natural language parser for common operator queries."""
        lowered = text.lower().strip()

        if lowered in ("/start", "/help", "help", "commands", "?"):
            return ParsedBotIntent(intent="help")

        # Match queries like "stock on John's ute", "inventory for ute-01", "check John's ute"
        ute_match = re.search(
            r"(?:stock|inventory|parts|check)\s+(?:on|for|in|at)?\s*(.+?)(?:\s+ute|\s+truck|\s+vehicle)?$",
            text,
            flags=re.IGNORECASE,
        )
        if ute_match and any(
            kw in lowered for kw in ("stock", "inventory", "ute", "truck", "parts")
        ):
            extracted = ute_match.group(1).strip(" ?.!")
            if not extracted.lower().endswith("ute") and "ute" in lowered:
                extracted = f"{extracted} Ute"
            return ParsedBotIntent(
                intent="check_ute_stock",
                ute_identifier=extracted,
            )

        if "ute" in lowered:
            possessive_match = re.search(
                r"([a-zA-Z0-9_\-]+(?:'s|’s)?\s*ute(?:-[a-zA-Z0-9]+)?)",
                text,
                flags=re.IGNORECASE,
            )
            ute_id = possessive_match.group(1).strip() if possessive_match else text
            return ParsedBotIntent(
                intent="check_ute_stock",
                ute_identifier=ute_id,
            )

        # Match queries like "status of Red Hill project", "Red Hill project status"
        proj_match = re.search(
            r"(?:status|summary|hours|info|progress)\s+(?:of|for|on)\s+(?:the\s+)?(.+?)(?:\s+project|\s+job)?$",
            text,
            flags=re.IGNORECASE,
        )
        if proj_match:
            return ParsedBotIntent(
                intent="check_project_status",
                project_identifier=proj_match.group(1).strip(" ?!."),
            )

        proj_suffix_match = re.search(
            r"^(.+?)\s+(?:project|job)(?:\s+status|\s+summary|\s+hours)?$",
            text,
            flags=re.IGNORECASE,
        )
        if proj_suffix_match:
            prefix = proj_suffix_match.group(1).strip(" ?!.")
            prefix = re.sub(
                r"^(?:what is the\s+|check\s+|show\s+|status of\s+)",
                "",
                prefix,
                flags=re.IGNORECASE,
            ).strip()
            return ParsedBotIntent(
                intent="check_project_status",
                project_identifier=prefix,
            )

        if lowered in ("projects", "active projects", "list projects", "/projects"):
            return ParsedBotIntent(intent="list_active_projects")

        return ParsedBotIntent(intent="unknown")

    async def _build_response_for_intent(self, parsed: ParsedBotIntent) -> str:
        """Query Firestore based on the parsed intent and format a human-readable response."""
        if parsed.intent == "check_ute_stock" and parsed.ute_identifier:
            _, display_label, items = (
                await self._firestore.find_ute_inventory_by_identifier(
                    parsed.ute_identifier
                )
            )
            label_clean = html.escape(display_label)
            if not items:
                return (
                    f"No inventory records found in Firestore for <b>{label_clean}</b>."
                )

            formatted_parts: list[str] = []
            low_stock_warnings: list[str] = []
            for item in items:
                qty = float(item.get("quantity", 0.0))
                threshold = float(item.get("reorder_threshold", 2.0))
                qty_str = f"{int(qty)}" if qty.is_integer() else f"{qty:g}"
                name = html.escape(str(item.get("name") or item.get("sku") or "Item"))
                formatted_parts.append(f"{qty_str}x {name}")
                if qty <= threshold:
                    low_stock_warnings.append(f"{name} ({qty_str} left)")

            parts_summary = ", ".join(formatted_parts)
            reply = f"Current stock for <b>{label_clean}</b>: {parts_summary}"
            if low_stock_warnings:
                reply += (
                    f"\n⚠️ <i>Low stock alert: {', '.join(low_stock_warnings)}</i>"
                )
            return reply

        if parsed.intent == "check_project_status" and parsed.project_identifier:
            project = await self._firestore.find_project_by_name_or_id(
                parsed.project_identifier
            )
            if not project:
                escaped_query = html.escape(parsed.project_identifier)
                return f"Could not find a project matching <b>{escaped_query}</b>."

            project_id = str(project.get("id"))
            project_name = html.escape(str(project.get("name") or project_id))
            client_name = html.escape(str(project.get("client_name") or "N/A"))
            status_val = html.escape(str(project.get("status") or "active"))
            ute_id = html.escape(str(project.get("ute_id") or "Unassigned"))

            shifts = await self._firestore.get_project_shifts(
                project_id, only_unbilled=False
            )
            total_hours = sum(float(s.get("hours") or 0.0) for s in shifts)
            unbilled_shifts = [s for s in shifts if not s.get("invoiced")]
            unbilled_hours = sum(float(s.get("hours") or 0.0) for s in unbilled_shifts)

            return (
                f"📋 <b>Project Status: {project_name}</b> (<code>{html.escape(project_id)}</code>)\n"
                f"• <b>Client:</b> {client_name}\n"
                f"• <b>Status:</b> {status_val}\n"
                f"• <b>Assigned Ute:</b> {ute_id}\n"
                f"• <b>Total Hours Logged:</b> {total_hours:g} hrs ({len(shifts)} shifts)\n"
                f"• <b>Unbilled Hours:</b> {unbilled_hours:g} hrs ({len(unbilled_shifts)} unbilled shifts)"
            )

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
                p_ute = html.escape(str(proj.get("ute_id") or "N/A"))
                lines.append(f"• <b>{p_name}</b> ({p_client}) — Ute: <code>{p_ute}</code>")
            return "\n".join(lines)

        return (
            "👋 <b>LaTrobe Irrigation Operations Bot</b>\n\n"
            "Try asking me:\n"
            "• <i>\"stock of 20mm pipe\n"
            "• <i>\"status of Red Hill project\"</i>\n"
            "• <i>\"active projects\"</i>"
        )

    async def process_incoming_message(
        self, chat_id: int, text: str
    ) -> dict[str, Any]:
        """Process an incoming operator message from the Telegram webhook:
        1. Evaluates the natural language text using an LLM (with heuristic fallback).
        2. Queries Cloud Firestore via `FirestoreService`.
        3. Formats the retrieved data into a clean, readable string.
        4. Calls `send_message` to return the answer to the user.
        """
        try:
            parsed_intent = await self._evaluate_text_with_llm(text)
            logger.info(
                "Evaluated Telegram message from chat_id=%s: intent=%s (ute=%s, project=%s)",
                chat_id,
                parsed_intent.intent,
                parsed_intent.ute_identifier,
                parsed_intent.project_identifier,
            )
            reply_text = await self._build_response_for_intent(parsed_intent)
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
        """Format and send a Telegram alert when Ute stock items fall at or below reorder threshold."""
        if not alerts:
            return {"ok": True, "skipped": True, "reason": "no_low_stock_items"}

        escaped_ute = html.escape(ute_id)
        lines = [
            f"⚠️ <b>Low Ute Stock Alert — Vehicle: {escaped_ute}</b>\n",
            "The following items are at or below their reorder threshold:\n",
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
            "Sent Telegram low-stock alert for Ute '%s' (%d item(s))",
            ute_id,
            len(alerts),
        )
        return result


telegram_service = TelegramService()
