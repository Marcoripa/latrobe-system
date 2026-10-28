from typing import Literal
from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    """Generic inbound customer message payload decoupled from the ingestion channel."""

    source: str = Field(..., description="Ingestion channel, e.g., 'sms', 'email', 'mock'")
    sender_id: str = Field(..., description="Sender identifier, e.g., '+61400000000' or 'client@domain.com'")
    text: str = Field(..., description="The raw customer request text")


class CustomerIdentityContext(BaseModel):
    """Resolved customer identity from Firestore/QuickBooks."""

    matched: bool = False
    flag: str = "NEW/UNKNOWN CLIENT"
    customer_id: str | None = None
    name: str = "NEW/UNKNOWN CLIENT"
    overdue_balance: float = 0.0
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    projects: list[dict] = Field(default_factory=list)


class ExtractedBookingDetails(BaseModel):
    """Structured booking proposal extracted by Gemini from the customer message."""

    intent: str = Field(default="book_job", description="Extracted intent of the customer request")
    urgency: Literal["low", "normal", "high", "emergency"] = Field(default="normal")
    summary: str = Field(..., description="Concise calendar event title")
    job_description: str = Field(..., description="Detailed job description from the request")
    project_id: str = Field(default="unassigned", description="Matched Firestore project ID or 'unassigned'")
    project_name: str = Field(default="Unassigned / General Service", description="Matched project name")
    location: str = Field(default="", description="Job site address or location")
    start_iso: str = Field(..., description="Proposed start time in ISO 8601 format (Australia/Melbourne)")
    end_iso: str = Field(..., description="Proposed end time in ISO 8601 format (Australia/Melbourne)")
    outbound_confirmation_text: str = Field(
        ...,
        description="Friendly confirmation message to send back to the customer if Confirm & Notify is chosen",
    )
