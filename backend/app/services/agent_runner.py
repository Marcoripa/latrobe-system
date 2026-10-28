import asyncio
import html
import inspect
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.services.calendar_service import calendar_service
from app.services.firestore_service import firestore_service

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

SYSTEM_PROMPT = (
    "You are the autonomous operations assistant for LaTrobe Irrigation's Telegram bot.\n"
    "Answer operator questions concisely, accurately, and professionally.\n"
    "You have access to tools that query real-time Firestore database records for master materials catalog/stock, "
    "project status (including hours by category and by operator, individual shifts with who logged them and when, materials/items used, location, and field notes), "
    "shift activity logs across projects/operators, and Google Calendar management (checking schedule and booking jobs).\n"
    "When answering user questions, use the tools dynamically. You can call multiple tools or chain multiple tool turns "
    "to answer complex, multi-step queries (e.g. check stock, check calendar availability, and book a job).\n\n"
    "CRITICAL RULES:\n"
    "1. All parts come from master stock. There is NO per-vehicle ('ute') stock tracking.\n"
    "2. Format your response strictly using Telegram HTML tags: <b>bold</b>, <i>italic</i>, <code>code</code>, <a>links</a>.\n"
    "3. Do NOT use Markdown formatting (no asterisks **, no backticks ```). Only use HTML tags supported by Telegram.\n"
    "4. Dynamically tailor what project information you present based on the user's question: if they ask a targeted question (e.g. only hours, only materials used, or who logged shifts and when), return just that relevant info; if they ask for a full review or summary of a project, include all available details (client, location, hours by category, hours by operator / who logged the shifts, shifts count, materials/items used with quantities, and field notes).\n"
    "5. CLARIFICATION & UNCERTAINTY POLICY (CRITICAL):\n"
    "   - Whenever a request is ambiguous or uncertain (e.g. `ambiguous: true` is returned with multiple candidate projects, multiple distinct materials match a vague query and the user asked about a single specific item, or a calendar booking request is missing date/time/project details), NEVER guess or silently pick one.\n"
    "   - Instead, clearly list the matching candidate options (or missing fields) and ask the user a concise clarifying question.\n"
    "   - If an entity is not found (`found: false`), never fabricate data; let the user know it wasn't found and suggest any similar candidates from `available_projects` or catalog results if applicable.\n"
    "   - Use the conversation history to resolve follow-up replies (e.g. if you asked 'Did you mean A or B?' and the user replies 'B', fulfill their original request for B).\n"
)

GEMINI_TOOLS_DECLARATIONS = [
    {
        "function_declarations": [
            {
                "name": "search_materials",
                "description": "Search the master materials catalog and stock levels in Firestore. All inventory items come from master stock.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {
                            "type": "STRING",
                            "description": "Keyword, SKU, or name of material to search for (e.g. '25mm poly pipe', 'valve')",
                        }
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_project_status",
                "description": "Get comprehensive project details from Firestore, including project ID, client info, site location, status, logged shift hours (total, unbilled, broken down by category and by operator), detailed shift records (who logged each shift, when, category, hours, items used, notes), aggregated materials/items used with quantities, and operator field notes.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "project_identifier": {
                            "type": "STRING",
                            "description": "Name or ID of the project (e.g. 'B. Shaw SP_26', 'Red Hill', '487')",
                        }
                    },
                    "required": ["project_identifier"],
                },
            },
            {
                "name": "get_shifts_activity",
                "description": "Query shift logs from the `shifts` collection to track who did what and when. Can filter by project name/ID, by operator name/email/UID (e.g. 'Andrew', 'Luke', 'Tom', 'Marco'), or list recent shifts across all projects.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "project_identifier": {
                            "type": "STRING",
                            "description": "Optional project name or ID to filter shifts by",
                        },
                        "operator_query": {
                            "type": "STRING",
                            "description": "Optional operator name, email, or UID to filter shifts by (e.g. 'Andrew', 'Luke')",
                        },
                    },
                },
            },
            {
                "name": "list_active_projects",
                "description": "Retrieves all currently active projects in Firestore.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {},
                },
            },
            {
                "name": "get_calendar_events",
                "description": "Retrieve scheduled events and bookings from Google Calendar within an ISO 8601 datetime window to check availability or view planned jobs.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "time_min_iso": {
                            "type": "STRING",
                            "description": "Start of the time window in ISO 8601 format with timezone offset, e.g. '2026-10-23T00:00:00+11:00'",
                        },
                        "time_max_iso": {
                            "type": "STRING",
                            "description": "End of the time window in ISO 8601 format with timezone offset, e.g. '2026-10-23T23:59:59+11:00'",
                        },
                    },
                    "required": ["time_min_iso", "time_max_iso"],
                },
            },
            {
                "name": "book_calendar_job",
                "description": "Book a new service, installation, quote, or inspection job in Google Calendar.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "summary": {
                            "type": "STRING",
                            "description": "Title or summary of the job, e.g. 'Installation - Red Hill' or 'Pump Repair - Tedesca'",
                        },
                        "start_iso": {
                            "type": "STRING",
                            "description": "Job start time in ISO 8601 format with timezone offset, e.g. '2026-10-24T09:00:00+11:00'",
                        },
                        "end_iso": {
                            "type": "STRING",
                            "description": "Job end time in ISO 8601 format with timezone offset, e.g. '2026-10-24T11:00:00+11:00'",
                        },
                        "location": {
                            "type": "STRING",
                            "description": "Site address or location for the job",
                        },
                        "description": {
                            "type": "STRING",
                            "description": "Detailed notes, client info, contact numbers, or materials needed",
                        },
                    },
                    "required": ["summary", "start_iso", "end_iso"],
                },
            },
        ]
    }
]

# Dynamic tool dispatch registry mapping tool names to service methods
TOOL_REGISTRY: dict[str, Any] = {
    "search_materials": firestore_service.search_materials,
    "get_project_status": firestore_service.get_project_status,
    "get_shifts_activity": firestore_service.get_shifts_activity,
    "list_active_projects": firestore_service.list_active_projects,
    "get_calendar_events": calendar_service.get_schedule,
    "book_calendar_job": calendar_service.schedule_job,
}


async def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dynamically dispatch and execute a registered tool function."""
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        logger.warning("Agent requested unregistered tool: '%s'", name)
        return {"error": f"Tool '{name}' is not registered."}

    try:
        if inspect.iscoroutinefunction(fn):
            res = await fn(**args)
        else:
            res = fn(**args)

        if isinstance(res, dict):
            return res
        elif isinstance(res, list):
            return {"items": res}
        else:
            return {"result": res}
    except Exception as exc:
        logger.exception("Error executing tool '%s' with args %s: %s", name, args, exc)
        return {"error": f"Failed to execute tool '{name}': {str(exc)}"}


class AgentRunner:
    """Autonomous ReAct agent executor using Gemini API and dynamic Firestore tools."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        max_turns: int = 5,
        timeout: float = 25.0,
    ) -> None:
        self.api_key = api_key or settings.GEMINI_API_KEY
        resolved_model = model_name or settings.LLM_MODEL or "gemini-3.8-flash"
        # If an obsolete 2.5 model is configured, default to gemini-3.8-flash
        if "gemini-2.5" in resolved_model:
            resolved_model = "gemini-3.8-flash"
        self.model_name = resolved_model
        self.max_turns = max_turns
        self.timeout = timeout
        self._http_client: httpx.AsyncClient | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Return a persistent httpx.AsyncClient for connection pooling and keep-alive."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._http_client

    def _build_generation_config(self) -> dict[str, Any]:
        """Configure low thinking overhead for fast operational tool routing and formatting."""
        model_lower = (self.model_name or "").lower()
        if "gemini-3" in model_lower:
            return {"thinkingConfig": {"thinkingLevel": "LOW"}}
        if "gemini-2.5" in model_lower:
            return {"thinkingConfig": {"thinkingBudget": 128}}
        return {}

    async def _build_active_projects_context(self) -> str:
        """Build a compact directory of active projects from the cached Firestore service."""
        try:
            active_projects = await firestore_service.list_projects(status_filter="active")
            if not active_projects:
                return "None currently active."
            lines: list[str] = []
            for p in active_projects[:25]:
                pid = str(p.get("id") or "")
                name = str(p.get("name") or pid)
                client = str(p.get("client_name") or "N/A")
                loc = str(p.get("location") or "")
                loc_suffix = f", Location: {loc}" if loc else ""
                lines.append(f"• [ID: {pid}] {name} (Client: {client}{loc_suffix})")
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("Non-critical failure building active projects context: %s", exc)
            return "Use `list_active_projects` tool if needed."

    async def run(
        self,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        """Execute the iterative ReAct loop:
        1. Receive user prompt and optional conversation history.
        2. Query Gemini with registered tools.
        3. If model returns terminal text, format and return it.
        4. If model returns functionCall(s), dynamically execute via TOOL_REGISTRY in parallel,
           append functionResponse turns under role 'user', and loop back to reason.
        5. Terminate gracefully on max_turns or unhandled error.
        """
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not configured; cannot run ReAct agent.")
            return (
                "👋 <b>LaTrobe Irrigation Operations Bot</b>\n\n"
                "I am ready to assist, but my AI reasoning service is not configured with an API key. "
                "Please configure <code>GEMINI_API_KEY</code>."
            )

        url = f"{GEMINI_API_BASE}/{self.model_name}:generateContent"

        contents: list[dict[str, Any]] = []
        if history:
            for turn in history:
                if turn.get("role") in ("user", "model") and "parts" in turn:
                    contents.append(turn)

        contents.append({"role": "user", "parts": [{"text": prompt.strip()}]})

        # Dynamically inject local datetime context and pre-computed 14-day calendar in Australia/Melbourne
        melbourne_tz = ZoneInfo("Australia/Melbourne")
        now_melbourne = datetime.now(melbourne_tz)
        current_time_str = now_melbourne.strftime("%A, %d %B %Y, %I:%M %p (%Z, %z)")
        current_iso = now_melbourne.isoformat()
        tz_offset = now_melbourne.strftime("%z")
        tz_offset_iso = f"{tz_offset[:3]}:{tz_offset[3:]}" if len(tz_offset) == 5 else "+11:00"

        upcoming_days: list[str] = []
        for offset in range(14):
            day_dt = now_melbourne + timedelta(days=offset)
            label = "Today" if offset == 0 else ("Tomorrow" if offset == 1 else day_dt.strftime("%a"))
            upcoming_days.append(f"{label} ({day_dt.strftime('%a %d %b')}): {day_dt.strftime('%Y-%m-%d')}")
        calendar_reference_str = " | ".join(upcoming_days)

        active_projects_context = await self._build_active_projects_context()

        dynamic_system_prompt = (
            f"{SYSTEM_PROMPT}\n"
            f"ACTIVE PROJECTS DIRECTORY (Use exact ID or name directly in `get_project_status` / `get_shifts_activity` without calling `list_active_projects` first unless asked to list all projects):\n"
            f"{active_projects_context}\n\n"
            f"TEMPORAL CONTEXT (CRITICAL):\n"
            f"• The current local date and time in Australia/Melbourne is: {current_time_str}\n"
            f"• Current ISO 8601 timestamp: {current_iso}\n"
            f"• Time zone: Australia/Melbourne (UTC offset {tz_offset_iso})\n"
            f"• Pre-computed 14-Day Calendar Reference: {calendar_reference_str}\n"
            f"Use this exact temporal context and 14-day reference table to resolve relative terms "
            f"such as 'today', 'tomorrow', 'this afternoon', 'next Monday', or 'next week'. "
            f"Always supply valid ISO 8601 strings with timezone offset ({tz_offset_iso}) when querying or booking calendar events."
        )

        client = self._get_http_client()
        gen_config = self._build_generation_config()

        for turn_idx in range(self.max_turns):
            payload: dict[str, Any] = {
                "system_instruction": {"parts": [{"text": dynamic_system_prompt}]},
                "contents": contents,
                "tools": GEMINI_TOOLS_DECLARATIONS,
            }
            if gen_config:
                payload["generationConfig"] = gen_config

            try:
                resp = await client.post(
                    url,
                    params={"key": self.api_key},
                    json=payload,
                )
            except httpx.RequestError as exc:
                logger.error("Network error during agent ReAct turn %d: %s", turn_idx + 1, exc)
                return (
                    "⚠️ <b>Network Error:</b> Could not reach AI service. Please check connectivity and try again."
                )

            if resp.status_code != 200:
                logger.error(
                    "Gemini API returned HTTP %d on turn %d: %s",
                    resp.status_code,
                    turn_idx + 1,
                    resp.text,
                )
                return (
                    "⚠️ <b>Service Error:</b> AI reasoning service encountered an error. Please try again later."
                )

            resp_data = resp.json()
            candidate = resp_data.get("candidates", [{}])[0]
            content = candidate.get("content", {})
            parts = content.get("parts", [])

            if not parts:
                logger.warning("Empty parts returned by Gemini on turn %d: %s", turn_idx + 1, resp_data)
                break

            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            # Step 3: Terminal text returned without function calls
            if not function_calls:
                text_parts = [p.get("text", "") for p in parts if "text" in p]
                final_text = "".join(text_parts).strip()
                if final_text:
                    return final_text
                break

            # Step 4: Model requested one or more tool calls
            logger.info(
                "ReAct Turn %d: Agent requested tool calls: %s",
                turn_idx + 1,
                [fc.get("name") for fc in function_calls],
            )

            # Append model turn preserving thought signatures, function calls, and parts
            contents.append({"role": "model", "parts": parts})

            # Execute all requested functions concurrently
            tool_results = await asyncio.gather(
                *(
                    execute_tool(fc.get("name", ""), fc.get("args") or {})
                    for fc in function_calls
                )
            )

            fn_response_parts: list[dict[str, Any]] = []
            for fc, tool_result in zip(function_calls, tool_results):
                tool_name = fc.get("name", "")
                call_id = fc.get("id")

                part_dict: dict[str, Any] = {
                    "name": tool_name,
                    "response": tool_result,
                }
                if call_id:
                    part_dict["id"] = call_id

                fn_response_parts.append({"functionResponse": part_dict})

            # Append function responses under role 'user' (required by Gemini Content spec)
            contents.append({"role": "user", "parts": fn_response_parts})

        logger.warning("Agent ReAct loop reached max turns (%d) for prompt: %r", self.max_turns, prompt)
        return (
            "⚠️ I reached the maximum reasoning steps while processing your request. "
            "Please try asking a more specific question."
        )


agent_runner = AgentRunner()
