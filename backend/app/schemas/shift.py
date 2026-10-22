from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ShiftCategory = Literal["Travel", "On-Site", "Shed/Prep"]


class ShiftItemUsed(BaseModel):
    """Material or inventory part consumed during a shift."""

    model_config = ConfigDict(populate_by_name=True)

    item_id: str = Field(..., min_length=1, description="Firestore inventory item ID")
    sku: str = Field(..., min_length=1, description="Part SKU or QuickBooks item code")
    name: str = Field(..., min_length=1, description="Human-readable part name")
    quantity: float = Field(..., gt=0, description="Quantity consumed during shift")
    unit_price: float | None = Field(
        default=None, ge=0, description="Optional unit price override for billing"
    )


class ShiftLogCreate(BaseModel):
    """Payload for logging an operator shift."""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., min_length=1, description="Target project ID")
    operator_id: str = Field(..., min_length=1, description="Operator Firebase UID")
    ute_id: str | None = Field(
        default=None, description="Optional Ute ID override for stock deduction"
    )
    hours: float = Field(..., gt=0, le=24, description="Total hours worked in shift")
    category: ShiftCategory = Field(
        ..., description="Shift activity category: Travel, On-Site, or Shed/Prep"
    )
    items_used: list[ShiftItemUsed] = Field(
        default_factory=list, description="List of inventory parts used on shift"
    )
    notes: str | None = Field(
        default=None, max_length=4000, description="Field notes or job observations"
    )
    photo_url: str | None = Field(
        default=None, description="Cloud Storage URL for site/work photo"
    )
    client_shift_id: str | None = Field(
        default=None,
        description="Optional client-generated ID for idempotent offline queue synchronization",
    )


class ShiftLogResponse(ShiftLogCreate):
    """Stored shift log record returned by the API."""

    id: str = Field(..., description="Firestore document ID of the shift log")
    invoiced: bool = Field(
        default=False, description="Whether this shift has been billed on an invoice"
    )
    invoice_id: str | None = Field(
        default=None, description="Associated QuickBooks invoice ID if billed"
    )
    invoiced_at: datetime | None = Field(
        default=None, description="Timestamp when the shift was marked invoiced"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the shift log was created",
    )
