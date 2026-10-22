"""Pydantic domain schemas for LaTrobe Irrigation Brain API."""

from app.schemas.inventory import (
    InventoryItem,
    LowStockAlert,
    ReorderThresholdUpdate,
    UteStockDeductionRequest,
    UteStockDeductionResult,
    UteStockItem,
)
from app.schemas.invoice import (
    DraftInvoiceSummary,
    InvoiceGenerationRequest,
    InvoiceLineItem,
)
from app.schemas.project import Project, ProjectCreate, ProjectStatus, ProjectUpdate
from app.schemas.shift import (
    ShiftCategory,
    ShiftItemUsed,
    ShiftLogCreate,
    ShiftLogResponse,
)
from app.schemas.telegram import (
    ParsedBotIntent,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)

__all__ = [
    "DraftInvoiceSummary",
    "InventoryItem",
    "InvoiceGenerationRequest",
    "InvoiceLineItem",
    "LowStockAlert",
    "ParsedBotIntent",
    "Project",
    "ProjectCreate",
    "ProjectStatus",
    "ProjectUpdate",
    "ReorderThresholdUpdate",
    "ShiftCategory",
    "ShiftItemUsed",
    "ShiftLogCreate",
    "ShiftLogResponse",
    "TelegramChat",
    "TelegramMessage",
    "TelegramUpdate",
    "TelegramUser",
    "UteStockDeductionRequest",
    "UteStockDeductionResult",
    "UteStockItem",
]
