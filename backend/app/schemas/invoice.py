from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InvoiceLineItem(BaseModel):
    """Normalized line item used to construct a QuickBooks Online draft invoice."""

    model_config = ConfigDict(populate_by_name=True)

    description: str = Field(..., min_length=1, description="Line item description")
    quantity: float = Field(..., gt=0, description="Quantity of hours or materials")
    unit_price: float = Field(..., ge=0, description="Unit rate or price")
    amount: float | None = Field(
        default=None, ge=0, description="Total line amount (computed as quantity * unit_price if omitted)"
    )
    item_ref_id: str = Field(
        default="1", description="QuickBooks ItemRef.value (defaults to '1' for standard service/item)"
    )
    item_ref_name: str | None = Field(
        default=None, description="Optional QuickBooks ItemRef.name"
    )
    sku: str | None = Field(default=None, description="Optional SKU reference")
    tax_code_ref: str | None = Field(
        default=None, description="Optional QuickBooks TaxCodeRef (e.g., 'GST' or 'NON')"
    )

    @property
    def computed_amount(self) -> float:
        if self.amount is not None:
            return round(self.amount, 2)
        return round(self.quantity * self.unit_price, 2)


class InvoiceGenerationRequest(BaseModel):
    """Request payload to generate a draft QuickBooks invoice from unbilled project shifts."""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., min_length=1, description="Firestore project ID to bill")
    qbo_customer_id: str | None = Field(
        default=None,
        description="QuickBooks CustomerRef ID (overrides project's qbo_customer_id if provided)",
    )
    hourly_rate: float = Field(
        default=125.0, gt=0, description="Hourly rate applied to labor shifts"
    )
    default_item_price: float = Field(
        default=25.0, ge=0, description="Fallback unit price for materials without an explicit price"
    )
    private_note: str | None = Field(
        default=None,
        max_length=4000,
        description="Internal QuickBooks PrivateNote attached to the draft invoice",
    )
    notify_manager: bool = Field(
        default=True,
        description="Whether to dispatch a Telegram notification once the draft is created",
    )


class DraftInvoiceSummary(BaseModel):
    """Summary returned after creating a draft invoice in QuickBooks Online."""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., description="Billed Firestore project ID")
    project_name: str = Field(..., description="Project name")
    client_name: str = Field(..., description="Client / farm name")
    qbo_customer_id: str = Field(..., description="QuickBooks CustomerRef ID")
    qbo_invoice_id: str = Field(..., description="QuickBooks Online Invoice Id")
    doc_number: str | None = Field(
        default=None, description="QuickBooks human-readable invoice DocNumber"
    )
    status: Literal["DRAFT"] = "DRAFT"
    email_status: str = Field(
        default="NeedToSend",
        description="QuickBooks EmailStatus ensuring draft is not auto-emailed",
    )
    total_amount: float = Field(..., ge=0, description="Total invoice amount")
    total_hours: float = Field(default=0.0, ge=0, description="Total labor hours billed")
    shift_ids: list[str] = Field(
        default_factory=list, description="List of Firestore shift IDs marked as invoiced"
    )
    line_items: list[InvoiceLineItem] = Field(
        default_factory=list, description="Line items included in the draft invoice"
    )
    qbo_review_url: str | None = Field(
        default=None, description="Direct URL to review the draft invoice in QuickBooks Online"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the draft invoice was generated",
    )
