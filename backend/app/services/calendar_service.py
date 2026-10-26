"""Google Calendar Service wrapping official Google Calendar v3 API using asyncio.to_thread."""

import asyncio
import logging
import os
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

from app.core.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_TIMEZONE = "Australia/Melbourne"


class CalendarServiceError(Exception):
    """Raised when Google Calendar API operations fail."""


class CalendarService:
    """Non-blocking Google Calendar service for fetching and scheduling jobs."""

    def __init__(
        self,
        service_account_file: str | None = None,
        calendar_id: str | None = None,
    ) -> None:
        self.service_account_file = (
            service_account_file or settings.GOOGLE_SERVICE_ACCOUNT_FILE
        )
        self.calendar_id = calendar_id or settings.GOOGLE_CALENDAR_ID
        self._service: Resource | None = None

    def _get_client(self) -> Resource:
        """Initialize or return the cached Google Calendar v3 API Resource."""
        if self._service is not None:
            return self._service

        key_path = self.service_account_file
        if not os.path.isabs(key_path):
            base_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            candidate_path = os.path.join(base_dir, key_path)
            if os.path.exists(candidate_path):
                key_path = candidate_path

        if not os.path.exists(key_path):
            logger.error("Google Calendar service account file not found: %s", key_path)
            raise CalendarServiceError(
                f"Google service account file not found at: {key_path}"
            )

        try:
            creds = service_account.Credentials.from_service_account_file(
                key_path, scopes=SCOPES
            )
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            return self._service
        except Exception as exc:
            logger.exception("Failed to build Google Calendar v3 client: %s", exc)
            raise CalendarServiceError(f"Failed to initialize Calendar client: {exc}") from exc

    async def get_schedule(
        self, time_min_iso: str, time_max_iso: str
    ) -> list[dict[str, Any]]:
        """Query events within an ISO datetime window (singleEvents=True, ordered by startTime).
        Returns a sanitized list of dicts with: id, summary, start, end, location.
        """

        def _fetch_sync() -> list[dict[str, Any]]:
            client = self._get_client()
            events_result = (
                client.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=time_min_iso,
                    timeMax=time_max_iso,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            raw_items = events_result.get("items", [])
            sanitized: list[dict[str, Any]] = []
            for item in raw_items:
                start_val = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date") or ""
                end_val = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date") or ""
                sanitized.append(
                    {
                        "id": item.get("id", ""),
                        "summary": item.get("summary", "Untitled Job / Event"),
                        "start": start_val,
                        "end": end_val,
                        "location": item.get("location", ""),
                    }
                )
            return sanitized

        try:
            return await asyncio.to_thread(_fetch_sync)
        except Exception as exc:
            logger.exception(
                "Error querying Google Calendar schedule (%s to %s): %s",
                time_min_iso,
                time_max_iso,
                exc,
            )
            return [{"error": f"Failed to retrieve calendar schedule: {str(exc)}"}]

    async def schedule_job(
        self,
        summary: str,
        location: str = "",
        description: str = "",
        start_iso: str = "",
        end_iso: str = "",
    ) -> dict[str, Any]:
        """Create and insert a new calendar job event set to time zone 'Australia/Melbourne'.
        Returns a confirmation dict containing status, event_id, summary, and htmlLink.
        """

        def _insert_sync() -> dict[str, Any]:
            client = self._get_client()
            event_body: dict[str, Any] = {
                "summary": summary,
                "location": location or "",
                "description": description or "",
                "start": {
                    "dateTime": start_iso,
                    "timeZone": DEFAULT_TIMEZONE,
                },
                "end": {
                    "dateTime": end_iso,
                    "timeZone": DEFAULT_TIMEZONE,
                },
            }
            created_event = (
                client.events()
                .insert(calendarId=self.calendar_id, body=event_body)
                .execute()
            )
            return {
                "status": "confirmed",
                "event_id": created_event.get("id", ""),
                "summary": created_event.get("summary", summary),
                "htmlLink": created_event.get("htmlLink", ""),
                "start": start_iso,
                "end": end_iso,
                "location": location or "",
            }

        try:
            return await asyncio.to_thread(_insert_sync)
        except Exception as exc:
            logger.exception("Error creating Google Calendar event '%s': %s", summary, exc)
            return {
                "status": "error",
                "error": f"Failed to schedule job in calendar: {str(exc)}",
            }


calendar_service = CalendarService()
