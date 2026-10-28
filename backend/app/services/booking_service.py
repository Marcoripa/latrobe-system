"""Human-in-the-Loop Copilot Booking Service for LaTrobe Irrigation.

Orchestrates:
1. Strict Identity Resolution against Firestore (`customers` and `projects` collections).
2. Gemini ReAct Agent reasoning (`gemini-3.8-flash` with fallback to configured `LLM_MODEL`)
   with temporal context (`Australia/Melbourne`) and Google Calendar availability checks.
3. Interactive Telegram Approval Card with Inline Keyboard (`confirm_notify`, `confirm_silent`, `reject`).
4. Dual-Database Sync on confirmation (Google Calendar event insertion + Firestore `projects/{project_id}/bookings/{booking_id}`).
5. Dynamic Outbound confirmation stub (`send_outbound_confirmation`).
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.schemas.booking import (
    CustomerIdentityContext,
    ExtractedBookingDetails,
    InboundMessage,
)
from app.services.agent_runner import (
    GEMINI_API_BASE,
    GEMINI_TOOLS_DECLARATIONS,
    execute_tool,
)
from app.services.calendar_service import CalendarService, calendar_service
from app.services.firestore_service import FirestoreService, firestore_service
from app.services.telegram_service import TelegramService, telegram_service

logger = logging.getLogger(__name__)

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
PREFERRED_BOOKING_MODEL = "gemini-3.8-flash"

BOOKING_AGENT_SYSTEM_PROMPT = (
    "You are the autonomous Booking Copilot for LaTrobe Irrigation.\n"
    "An inbound message or email has arrived. Your job is to:\n"
    "1. Determine whether this message is an actual job/service/booking inquiry (`intent: \"book_job\"`) or an unrelated email such as a newsletter, automated receipt, spam, or general non-booking notice (`intent: \"ignore_non_booking\"`).\n"
    "2. Analyze the enriched customer identity context (whether a matched QBO/Firestore customer or NEW/UNKNOWN CLIENT).\n"
    "3. Use `get_calendar_events` to check Google Calendar availability around the customer's requested date/time AND respect any TENTATIVELY HELD PENDING PROPOSALS listed in the prompt so you propose a conflict-free slot during standard working hours (07:30 to 17:00 Australia/Melbourne, unless emergency).\n"
    "4. Match the request to the appropriate `project_id` from the customer's projects (or call `list_active_projects` if needed; use 'unassigned' if NEW/UNKNOWN CLIENT or no project matches).\n"
    "5. Output ONLY a valid JSON object matching this exact schema (no markdown fences, no extra prose):\n"
    "{\n"
    '  "intent": "book_job" | "ignore_non_booking",\n'
    '  "urgency": "low" | "normal" | "high" | "emergency",\n'
    '  "summary": "Short calendar title (e.g. Irrigation Repair - Ben Shaw)",\n'
    '  "job_description": "Detailed job description extracted from the customer request",\n'
    '  "project_id": "Firestore project ID or \'unassigned\'",\n'
    '  "project_name": "Project name or \'Unassigned / New Client\'",\n'
    '  "location": "Site address or location",\n'
    '  "start_iso": "YYYY-MM-DDTHH:MM:SS+11:00",\n'
    '  "end_iso": "YYYY-MM-DDTHH:MM:SS+11:00",\n'
    '  "outbound_confirmation_text": "Friendly SMS/Email confirmation message to send to the customer confirming the date and time"\n'
    "}\n"
    "CRITICAL: Do NOT call `create_calendar_event` yourself during proposal generation! "
    "The event is only inserted after the admin presses Confirm on the Telegram card."
)


async def send_outbound_confirmation(
    source: str,
    sender_id: str,
    text: str,
    subject: str = "Booking Confirmation - LaTrobe Irrigation",
) -> dict[str, Any]:
    """Send a booking confirmation back to the customer via the originating channel.
    Uses Gmail SMTP when `source == 'email'` and Gmail credentials are configured;
    logs as a stub for SMS/other channels.
    """
    logger.info(
        "[OUTBOUND CONFIRMATION] source=%s | sender_id=%s | text=%r",
        source,
        sender_id,
        text,
    )
    if source.lower() == "email" and "@" in (sender_id or ""):
        from app.services.email_service import email_service

        if email_service.is_configured:
            smtp_res = await email_service.send_email(
                to_address=sender_id.strip(),
                subject=subject,
                body=text,
            )
            return {
                "status": "email_sent" if smtp_res.get("sent") else "email_failed",
                "source": source,
                "sender_id": sender_id,
                "text": text,
                "smtp": smtp_res,
            }

    return {
        "status": "stub_logged",
        "source": source,
        "sender_id": sender_id,
        "text": text,
    }


class BookingService:
    """Orchestrates inbound booking requests and Telegram callback confirmations."""

    def __init__(
        self,
        fs: FirestoreService | None = None,
        cal: CalendarService | None = None,
        tg: TelegramService | None = None,
    ) -> None:
        self._fs = fs or firestore_service
        self._cal = cal or calendar_service
        self._tg = tg or telegram_service
        self._processing_proposals: set[str] = set()

    async def resolve_customer_identity(
        self, sender_id: str
    ) -> CustomerIdentityContext:
        """Query Firestore `customers` collection for a matching profile by phone, email, or ID."""
        customer_doc = await self._fs.find_customer_by_sender(sender_id)
        if not customer_doc:
            logger.info(
                "No customer match found for sender_id='%s' -> Flagging as NEW/UNKNOWN CLIENT",
                sender_id,
            )
            return CustomerIdentityContext(
                matched=False,
                flag="NEW/UNKNOWN CLIENT",
                customer_id=None,
                name="NEW/UNKNOWN CLIENT",
                overdue_balance=0.0,
            )

        cid = str(
            customer_doc.get("customer_id")
            or customer_doc.get("qbo_customer_id")
            or customer_doc.get("qbo_id")
            or customer_doc.get("id")
        )
        cname = str(
            customer_doc.get("display_name")
            or customer_doc.get("name")
            or f"Customer {cid}"
        )
        try:
            overdue = round(float(customer_doc.get("overdue_balance") or 0.0), 2)
        except (TypeError, ValueError):
            overdue = 0.0

        projects = await self._fs.get_customer_projects(cid)
        project_summaries = [
            {
                "project_id": str(p.get("id")),
                "name": p.get("name"),
                "status": p.get("status", "active"),
                "location": p.get("location") or p.get("address") or "",
            }
            for p in projects
        ]

        return CustomerIdentityContext(
            matched=True,
            flag="EXISTING CLIENT",
            customer_id=cid,
            name=cname,
            overdue_balance=overdue,
            email=customer_doc.get("email"),
            phone=customer_doc.get("phone") or customer_doc.get("mobile"),
            address=customer_doc.get("address") or customer_doc.get("location"),
            projects=project_summaries,
        )

    async def _run_gemini_booking_agent(
        self,
        msg: InboundMessage,
        identity: CustomerIdentityContext,
    ) -> ExtractedBookingDetails:
        """Run the Gemini ReAct loop with enriched customer context and temporal context
        to extract structured booking proposal details.
        """
        now_melb = datetime.now(MELBOURNE_TZ)
        default_start = (now_melb + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        default_end = default_start + timedelta(hours=2)

        default_project_id = (
            identity.projects[0]["project_id"]
            if identity.projects
            else "unassigned"
        )
        default_project_name = (
            identity.projects[0]["name"]
            if identity.projects
            else (f"{identity.name} - General" if identity.matched else "NEW/UNKNOWN CLIENT")
        )
        default_location = (
            (identity.projects[0].get("location") if identity.projects else None)
            or identity.address
            or ""
        )

        if not settings.GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY not set; using deterministic fallback booking extraction.")
            return ExtractedBookingDetails(
                intent="book_job",
                urgency="normal",
                summary=f"Service Request - {identity.name}",
                job_description=msg.text.strip(),
                project_id=default_project_id,
                project_name=default_project_name,
                location=default_location,
                start_iso=default_start.isoformat(),
                end_iso=default_end.isoformat(),
                outbound_confirmation_text=(
                    f"Hi {identity.name if identity.matched else 'there'}, "
                    f"LaTrobe Irrigation has scheduled your service for "
                    f"{default_start.strftime('%A %d %b at %I:%M %p')}."
                ),
            )

        # Filter tools so the booking proposal agent can read calendar/projects/materials but cannot prematurely create events
        read_only_declarations = [
            decl
            for decl in GEMINI_TOOLS_DECLARATIONS[0]["function_declarations"]
            if decl["name"] != "create_calendar_event"
        ]
        tools_payload = [{"function_declarations": read_only_declarations}]

        current_time_str = now_melb.strftime("%A, %d %B %Y, %I:%M %p (%Z, %z)")
        if identity.matched:
            customer_block = (
                f"CUSTOMER STATUS: MATCHED EXISTING CLIENT\n"
                f"• QBO Customer ID: {identity.customer_id}\n"
                f"• Customer Name: {identity.name}\n"
                f"• Overdue Balance: ${identity.overdue_balance:,.2f} AUD\n"
                f"• Known Address: {identity.address or 'Not on file'}\n"
                f"• Associated Projects in Firestore: {json.dumps(identity.projects)}"
            )
        else:
            customer_block = (
                "CUSTOMER STATUS: NEW/UNKNOWN CLIENT\n"
                "• No matching customer profile found in Firestore/QBO for this sender.\n"
                "• Use project_id='unassigned' unless the message explicitly names an existing active project."
            )

        # Fetch any unconfirmed pending proposals so concurrent requests don't propose overlapping slots
        pending_proposals = await self._fs.list_pending_booking_proposals()
        if pending_proposals:
            pending_slots_block = (
                "TENTATIVELY HELD SLOTS (PENDING ADMIN APPROVAL — DO NOT OVERLAP WITH THESE):\n"
                f"{json.dumps(pending_proposals, indent=2)}"
            )
        else:
            pending_slots_block = (
                "TENTATIVELY HELD SLOTS (PENDING ADMIN APPROVAL): None"
            )

        user_prompt = (
            f"INBOUND BOOKING REQUEST:\n"
            f"• Source Channel: {msg.source}\n"
            f"• Sender ID: {msg.sender_id}\n"
            f"• Current Local Time (Australia/Melbourne): {current_time_str} ({now_melb.isoformat()})\n\n"
            f"{customer_block}\n\n"
            f"{pending_slots_block}\n\n"
            f"RAW CUSTOMER MESSAGE:\n"
            f"\"\"\"{msg.text}\"\"\"\n\n"
            f"Check the calendar if needed, avoid overlapping both confirmed calendar events and tentatively held pending proposals, and return the JSON booking proposal."
        )

        contents: list[dict[str, Any]] = [
            {"role": "user", "parts": [{"text": user_prompt}]}
        ]

        # Try preferred model first, falling back to settings.LLM_MODEL if 404
        candidate_models = [PREFERRED_BOOKING_MODEL]
        if settings.LLM_MODEL and settings.LLM_MODEL not in candidate_models:
            candidate_models.append(settings.LLM_MODEL)
        if "gemini-2.5-flash" not in candidate_models:
            candidate_models.append("gemini-2.5-flash")

        active_model = candidate_models[0]

        async with httpx.AsyncClient(timeout=30.0) as client:
            for turn_idx in range(5):
                payload: dict[str, Any] = {
                    "system_instruction": {
                        "parts": [{"text": BOOKING_AGENT_SYSTEM_PROMPT}]
                    },
                    "contents": contents,
                    "tools": tools_payload,
                }

                resp = None
                for model_to_try in candidate_models:
                    url = f"{GEMINI_API_BASE}/{model_to_try}:generateContent"
                    try:
                        r = await client.post(
                            url,
                            params={"key": settings.GEMINI_API_KEY},
                            json=payload,
                        )
                        if r.status_code == 404 and model_to_try != candidate_models[-1]:
                            logger.info(
                                "Model '%s' returned 404; falling back to next candidate model.",
                                model_to_try,
                            )
                            continue
                        active_model = model_to_try
                        resp = r
                        break
                    except httpx.RequestError as exc:
                        logger.error("Network error calling Gemini (%s): %s", model_to_try, exc)
                        break

                # Lock onto active_model for subsequent turns
                candidate_models = [active_model]

                if resp is None or resp.status_code != 200:
                    logger.warning(
                        "Gemini call failed during booking extraction (status=%s); using fallback.",
                        getattr(resp, "status_code", None),
                    )
                    break

                resp_data = resp.json()
                candidate = resp_data.get("candidates", [{}])[0]
                parts = candidate.get("content", {}).get("parts", [])
                if not parts:
                    break

                function_calls = [p["functionCall"] for p in parts if "functionCall" in p]
                if not function_calls:
                    raw_text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
                    parsed = self._parse_gemini_json(
                        raw_text,
                        identity=identity,
                        msg=msg,
                        default_project_id=default_project_id,
                        default_project_name=default_project_name,
                        default_location=default_location,
                        default_start=default_start,
                        default_end=default_end,
                    )
                    if parsed:
                        return parsed
                    break

                contents.append({"role": "model", "parts": parts})
                fn_response_parts: list[dict[str, Any]] = []
                for fc in function_calls:
                    tool_name = fc.get("name", "")
                    tool_args = fc.get("args") or {}
                    call_id = fc.get("id")
                    tool_result = await execute_tool(tool_name, tool_args)
                    part_dict: dict[str, Any] = {
                        "name": tool_name,
                        "response": tool_result,
                    }
                    if call_id:
                        part_dict["id"] = call_id
                    fn_response_parts.append({"functionResponse": part_dict})

                contents.append({"role": "user", "parts": fn_response_parts})

        return ExtractedBookingDetails(
            intent="book_job",
            urgency="normal",
            summary=f"Service Request - {identity.name}",
            job_description=msg.text.strip(),
            project_id=default_project_id,
            project_name=default_project_name,
            location=default_location,
            start_iso=default_start.isoformat(),
            end_iso=default_end.isoformat(),
            outbound_confirmation_text=(
                f"Hi {identity.name if identity.matched else 'there'}, "
                f"LaTrobe Irrigation has scheduled your job for "
                f"{default_start.strftime('%A %d %b at %I:%M %p')}."
            ),
        )

    @staticmethod
    def _parse_gemini_json(
        raw_text: str,
        *,
        identity: CustomerIdentityContext,
        msg: InboundMessage,
        default_project_id: str,
        default_project_name: str,
        default_location: str,
        default_start: datetime,
        default_end: datetime,
    ) -> ExtractedBookingDetails | None:
        """Extract and validate JSON from Gemini's response text."""
        cleaned = raw_text.strip()
        fence_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(0)

        try:
            data = json.loads(cleaned)
        except Exception as exc:
            logger.warning("Failed to parse booking JSON from Gemini output: %s", exc)
            return None

        urgency = str(data.get("urgency") or "normal").lower()
        if urgency not in ("low", "normal", "high", "emergency"):
            urgency = "normal"

        return ExtractedBookingDetails(
            intent=str(data.get("intent") or "book_job"),
            urgency=urgency,  # type: ignore[arg-type]
            summary=str(data.get("summary") or f"Service - {identity.name}"),
            job_description=str(data.get("job_description") or msg.text.strip()),
            project_id=str(data.get("project_id") or default_project_id),
            project_name=str(data.get("project_name") or default_project_name),
            location=str(data.get("location") or default_location),
            start_iso=str(data.get("start_iso") or default_start.isoformat()),
            end_iso=str(data.get("end_iso") or default_end.isoformat()),
            outbound_confirmation_text=str(
                data.get("outbound_confirmation_text")
                or (
                    f"Hi {identity.name if identity.matched else 'there'}, "
                    f"your booking with LaTrobe Irrigation is confirmed for "
                    f"{data.get('start_iso') or default_start.isoformat()}."
                )
            ),
        )

    async def process_booking_request(self, msg: InboundMessage) -> dict[str, Any]:
        """Core asynchronous orchestrator for inbound booking requests:
        1. Strict Identity Resolution against QBO/Firestore `customers`.
        2. Gemini ReAct Agent processing with customer & Melbourne temporal context.
        3. Persist pending proposal in Firestore (`booking_proposals/{proposal_id}`).
        4. Send interactive approval card with Inline Keyboard to Telegram admin.
        """
        # 1. Strict Identity Resolution
        identity = await self.resolve_customer_identity(msg.sender_id)

        # 2. Gemini ReAct Agent Processing
        extracted = await self._run_gemini_booking_agent(msg, identity)

        if extracted.intent == "ignore_non_booking":
            logger.info(
                "Inbound message from '%s' classified as non-booking; skipping Telegram proposal.",
                msg.sender_id,
            )
            return {
                "status": "ignored_non_booking",
                "proposal_id": None,
                "customer": identity.model_dump(),
            }

        # Backfill QBO Customer ID from the matched project if sender_id was a new/unmatched phone or email
        resolved_qbo_customer_id = identity.customer_id
        resolved_customer_name = " ".join(identity.name.split())
        resolved_matched = identity.matched
        resolved_flag = identity.flag
        resolved_overdue = identity.overdue_balance

        if not resolved_qbo_customer_id and extracted.project_id and extracted.project_id != "unassigned":
            proj_doc = await self._fs.get_project(extracted.project_id)
            if proj_doc:
                resolved_qbo_customer_id = str(
                    proj_doc.get("client_id")
                    or proj_doc.get("parent_customer_id")
                    or proj_doc.get("qbo_customer_id")
                    or ""
                ).strip() or None
                if resolved_customer_name == "NEW/UNKNOWN CLIENT" and proj_doc.get("client_name"):
                    resolved_customer_name = " ".join(str(proj_doc["client_name"]).split())
                if resolved_qbo_customer_id:
                    resolved_matched = True
                    resolved_flag = "EXISTING CLIENT"
                    cust_doc = await self._fs.find_customer_by_sender(resolved_qbo_customer_id)
                    if cust_doc and cust_doc.get("overdue_balance") is not None:
                        try:
                            resolved_overdue = round(float(cust_doc["overdue_balance"]), 2)
                        except (TypeError, ValueError):
                            pass

        # 3. Persist Proposal
        proposal_id = uuid.uuid4().hex[:12]
        proposal_record: dict[str, Any] = {
            "proposal_id": proposal_id,
            "status": "pending_approval",
            "source": msg.source,
            "sender_id": msg.sender_id,
            "raw_text": msg.text,
            "customer_matched": resolved_matched,
            "customer_flag": resolved_flag,
            "qbo_customer_id": resolved_qbo_customer_id or "UNKNOWN",
            "customer_name": resolved_customer_name,
            "overdue_balance": resolved_overdue,
            "intent": extracted.intent,
            "urgency": extracted.urgency,
            "summary": extracted.summary,
            "job_description": extracted.job_description,
            "project_id": extracted.project_id,
            "project_name": extracted.project_name,
            "location": extracted.location,
            "start_iso": extracted.start_iso,
            "end_iso": extracted.end_iso,
            "proposed_slot": self._tg.format_proposed_slot(
                extracted.start_iso, extracted.end_iso
            ),
            "outbound_confirmation_text": extracted.outbound_confirmation_text,
        }
        await self._fs.save_booking_proposal(proposal_id, proposal_record)

        # 4. Send Interactive Telegram Approval Card
        tg_result = await self._tg.send_booking_proposal_card(
            proposal_id=proposal_id,
            proposal=proposal_record,
        )

        return {
            "status": "proposal_sent",
            "proposal_id": proposal_id,
            "customer": identity.model_dump(),
            "proposal": proposal_record,
            "telegram_result": tg_result,
        }

    async def handle_callback_query(
        self,
        callback_query_id: str,
        callback_data: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Process admin button presses from the Telegram Inline Keyboard:
        - `confirm_notify|<proposal_id>`: Dual-sync to GCal + Firestore (`projects/{project_id}/bookings/{booking_id}`)
          AND trigger `send_outbound_confirmation(source, sender_id, text)`.
        - `confirm_silent|<proposal_id>`: Dual-sync to GCal + Firestore silently.
        - `reject|<proposal_id>`: Mark proposal rejected and update Telegram card.
        """
        if "|" not in (callback_data or ""):
            await self._tg.answer_callback_query(
                callback_query_id, text="Invalid callback action."
            )
            return {"ok": False, "reason": "invalid_callback_data"}

        action, proposal_id = callback_data.split("|", 1)
        target_chat_id = chat_id if chat_id is not None else settings.TELEGRAM_ADMIN_CHAT_ID

        # 0. In-memory concurrency guard against rapid double-taps
        if proposal_id in self._processing_proposals:
            await self._tg.answer_callback_query(
                callback_query_id,
                text="⏳ Already processing this booking...",
            )
            return {"ok": False, "reason": "already_processing", "proposal_id": proposal_id}

        self._processing_proposals.add(proposal_id)
        try:
            # 1. Instantly deactivate/remove the Inline Keyboard buttons on Telegram before doing heavy work
            if target_chat_id and message_id is not None:
                try:
                    await self._tg.edit_message_reply_markup(
                        chat_id=target_chat_id,
                        message_id=message_id,
                        reply_markup={"inline_keyboard": []},
                    )
                except Exception as markup_exc:
                    logger.debug("Non-critical error clearing inline keyboard: %s", markup_exc)

            proposal = await self._fs.get_booking_proposal(proposal_id)
            if not proposal:
                await self._tg.answer_callback_query(
                    callback_query_id,
                    text="Booking proposal not found.",
                    show_alert=True,
                )
                return {"ok": False, "reason": "proposal_not_found", "proposal_id": proposal_id}

            # 2. Idempotency check in Firestore: prevent multiple confirmations if already processed
            current_status = str(proposal.get("status") or "pending_approval")
            if current_status != "pending_approval":
                await self._tg.answer_callback_query(
                    callback_query_id,
                    text=f"Already {current_status}!",
                )
                return {
                    "ok": False,
                    "reason": "already_processed",
                    "status": current_status,
                    "proposal_id": proposal_id,
                }

            # Handle Reject
            if action == "reject":
                await self._fs.update_booking_proposal(
                    proposal_id,
                    {"status": "rejected", "rejected_by": user_id},
                )
                await self._tg.answer_callback_query(
                    callback_query_id, text="❌ Booking proposal rejected."
                )
                if target_chat_id and message_id is not None:
                    updated_text = self._tg.format_booking_proposal_text(
                        proposal, final_status="❌ Rejected by Admin"
                    )
                    await self._tg.edit_message(
                        chat_id=target_chat_id,
                        message_id=message_id,
                        text=updated_text,
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": []},
                    )
                return {"ok": True, "status": "rejected", "proposal_id": proposal_id}

            # Handle Confirm (Notify or Silent)
            if action in ("confirm_notify", "confirm_silent"):
                # Mark as processing in Firestore immediately
                await self._fs.update_booking_proposal(
                    proposal_id, {"status": "processing"}
                )

                # 1. Insert event into Google Calendar and retrieve new eventId
                cal_description = (
                    f"Client: {proposal.get('customer_name')} (QBO ID: {proposal.get('qbo_customer_id')})\n"
                    f"Channel: {proposal.get('source')} ({proposal.get('sender_id')})\n"
                    f"Job Description: {proposal.get('job_description')}"
                )
                cal_res = await self._cal.schedule_job(
                    summary=str(proposal.get("summary") or "LaTrobe Irrigation Booking"),
                    location=str(proposal.get("location") or ""),
                    description=cal_description,
                    start_iso=str(proposal.get("start_iso") or ""),
                    end_iso=str(proposal.get("end_iso") or ""),
                )
                event_id = str(cal_res.get("event_id") or "")

                # 2. Create document in `projects/{project_id}/bookings/{booking_id}` in Firestore
                project_id = str(proposal.get("project_id") or "unassigned")
                booking_doc_payload: dict[str, Any] = {
                    "qbo_customer_id": str(proposal.get("qbo_customer_id") or "UNKNOWN"),
                    "customer_name": proposal.get("customer_name"),
                    "job_description": str(proposal.get("job_description") or ""),
                    "description": str(proposal.get("job_description") or ""),
                    "summary": proposal.get("summary"),
                    "status": "scheduled",
                    "start_time": proposal.get("start_iso"),
                    "end_time": proposal.get("end_iso"),
                    "eventId": event_id,
                    "gcal_event_id": event_id,
                    "gcal_html_link": cal_res.get("htmlLink", ""),
                    "location": proposal.get("location") or "",
                    "source": proposal.get("source"),
                    "sender_id": proposal.get("sender_id"),
                    "proposal_id": proposal_id,
                    "notified_client": action == "confirm_notify",
                }
                booking_record = await self._fs.create_project_booking(
                    project_id=project_id,
                    booking_data=booking_doc_payload,
                    booking_id=proposal_id,
                )

                await self._fs.update_booking_proposal(
                    proposal_id,
                    {
                        "status": "scheduled",
                        "eventId": event_id,
                        "booking_id": booking_record.get("booking_id", proposal_id),
                        "confirmed_mode": action,
                        "confirmed_by": user_id,
                    },
                )

                # 3. Dynamic Outbound Confirmation if `confirm_notify` was pressed
                outbound_result = None
                if action == "confirm_notify":
                    outbound_result = await send_outbound_confirmation(
                        source=str(proposal.get("source") or "unknown"),
                        sender_id=str(proposal.get("sender_id") or ""),
                        text=str(proposal.get("outbound_confirmation_text") or ""),
                    )
                    status_label = "✅ Scheduled and Saved (Client Notified)"
                    toast_text = "✅ Scheduled & client notification triggered!"
                else:
                    status_label = "✅ Scheduled and Saved (Silent)"
                    toast_text = "🤫 Scheduled silently!"

                if event_id:
                    status_label += f" | GCal Event ID: <code>{event_id}</code>"

                await self._tg.answer_callback_query(
                    callback_query_id, text=toast_text
                )

                # 4. Edit original Telegram message to display final status
                if target_chat_id and message_id is not None:
                    updated_text = self._tg.format_booking_proposal_text(
                        proposal, final_status=status_label
                    )
                    await self._tg.edit_message(
                        chat_id=target_chat_id,
                        message_id=message_id,
                        text=updated_text,
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": []},
                    )

                return {
                    "ok": True,
                    "status": "scheduled",
                    "mode": action,
                    "proposal_id": proposal_id,
                    "project_id": project_id,
                    "booking_id": booking_record.get("booking_id", proposal_id),
                    "eventId": event_id,
                    "outbound": outbound_result,
                }

            await self._tg.answer_callback_query(
                callback_query_id, text=f"Unknown action: {action}"
            )
            return {"ok": False, "reason": "unknown_action", "action": action}
        finally:
            self._processing_proposals.discard(proposal_id)


booking_service = BookingService()


async def process_booking_request(msg: InboundMessage) -> dict[str, Any]:
    """Top-level asynchronous orchestrator function for InboundMessage payloads."""
    return await booking_service.process_booking_request(msg)
