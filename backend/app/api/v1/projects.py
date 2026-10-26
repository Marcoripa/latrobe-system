import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.core.security import UserContext, get_current_user, require_manager
from app.schemas.invoice import DraftInvoiceSummary, InvoiceLineItem
from app.schemas.project import Project, ProjectCreate
from app.services.firestore_service import FirestoreServiceError, firestore_service
from app.services.quickbooks_service import QuickBooksAPIError, quickbooks_service
from app.services.telegram_service import TelegramNotificationError, telegram_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["Projects"])


@router.post(
    "",
    response_model=Project,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new irrigation project",
)
async def create_project(
    payload: ProjectCreate,
    _current_user: UserContext = Depends(get_current_user),
) -> Project:
    """Create and persist a new active irrigation project in Firestore."""
    try:
        created = await firestore_service.create_project(payload)
        return Project(**created)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get(
    "",
    summary="List projects (defaults to active projects)",
)
async def list_projects(
    project_status: str | None = Query(
        default="active",
        alias="status",
        description="Filter projects by lifecycle status (e.g. 'active', 'closed')",
    ),
    current_user: UserContext = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return projects from Firestore, filtered to the operator's assignments unless manager/admin."""
    try:
        projects = await firestore_service.list_projects(status=project_status)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    if current_user.is_manager_or_admin:
        return projects

    filtered: list[dict[str, Any]] = []
    for proj in projects:
        assigned_ops = proj.get("assignedOperators") or proj.get("assigned_operators")
        assigned_to = proj.get("assignedTo") or proj.get("assigned_to")
        if isinstance(assigned_ops, list) and len(assigned_ops) > 0:
            if current_user.uid in assigned_ops:
                filtered.append(proj)
        elif assigned_to:
            if assigned_to == current_user.uid:
                filtered.append(proj)
        else:
            filtered.append(proj)
    return filtered



class ProjectCloseRequest(BaseModel):
    """Optional configuration overrides when closing a project and generating a draft invoice."""

    qbo_customer_id: str | None = Field(
        default=None,
        description="QuickBooks CustomerRef ID (overrides the project's stored qbo_customer_id)",
    )
    hourly_rate: float | None = Field(
        default=None,
        gt=0,
        description="Override hourly labor rate (AUD). Defaults to project hourly_rate or $125.00/hr.",
    )
    default_item_price: float = Field(
        default=25.0,
        ge=0,
        description="Fallback unit price (AUD) for materials without an explicit unit_price",
    )
    private_note: str | None = Field(
        default=None,
        max_length=4000,
        description="Custom internal note appended to QuickBooks PrivateNote",
    )
    notify_manager: bool = Field(
        default=True,
        description="Send Telegram alert to manager with QuickBooks review link",
    )


async def execute_project_close_and_draft_invoice(
    project_id: str,
    close_options: ProjectCloseRequest | None = None,
) -> DraftInvoiceSummary:
    """Core workflow that aggregates unbilled project shifts, builds labor and material line items,
    creates a QuickBooks Online DRAFT invoice (`EmailStatus="NeedToSend"`), marks shifts as invoiced,
    updates project status, and sends a Telegram alert to the manager.
    """
    opts = close_options or ProjectCloseRequest()

    project_doc = await firestore_service.get_project(project_id)
    if not project_doc:
        raise LookupError(f"Project '{project_id}' was not found in Firestore.")

    unbilled_shifts = await firestore_service.get_unbilled_shifts(project_id)
    if not unbilled_shifts:
        raise ValueError(
            f"No unbilled shifts found for project '{project_id}'. Nothing to invoice."
        )

    project_name = str(project_doc.get("name") or f"Project {project_id}")
    client_name = str(project_doc.get("client_name") or "LaTrobe Client")
    qbo_customer_id = str(
        opts.qbo_customer_id
        or project_doc.get("qbo_customer_id")
        or project_doc.get("client_id")
        or "1"
    )
    hourly_rate = float(
        opts.hourly_rate
        if opts.hourly_rate is not None
        else project_doc.get("hourly_rate", 125.0)
    )

    # 1. Aggregate labor hours by category and materials by (sku, name, unit_price)
    hours_by_category: dict[str, float] = defaultdict(float)
    aggregated_materials: dict[tuple[str, str, float], dict[str, Any]] = {}
    shift_ids: list[str] = []
    notes_summary: list[str] = []
    total_hours = 0.0

    for shift in unbilled_shifts:
        shift_id = str(shift.get("id", ""))
        if shift_id:
            shift_ids.append(shift_id)

        category = str(shift.get("category") or "On-Site")
        hours = float(shift.get("hours") or 0.0)
        if hours > 0:
            hours_by_category[category] += hours
            total_hours += hours

        if shift.get("notes"):
            operator_id = shift.get("operator_id", "op")
            notes_summary.append(f"[{category} - {operator_id}] {shift['notes']}")

        for raw_item in shift.get("items_used") or []:
            sku = str(raw_item.get("sku") or raw_item.get("item_id") or "PART")
            name = str(raw_item.get("name") or sku)
            qty = float(raw_item.get("quantity") or 0.0)
            if qty <= 0:
                continue
            raw_price = raw_item.get("unit_price")
            unit_price = (
                float(raw_price)
                if raw_price is not None
                else float(opts.default_item_price)
            )
            key = (sku, name, unit_price)
            if key not in aggregated_materials:
                aggregated_materials[key] = {
                    "item_id": str(raw_item.get("item_id") or "1"),
                    "sku": sku,
                    "name": name,
                    "quantity": 0.0,
                    "unit_price": unit_price,
                }
            aggregated_materials[key]["quantity"] += qty

    # 2. Build labor lines + material lines
    invoice_lines: list[InvoiceLineItem] = []
    for category, cat_hours in hours_by_category.items():
        rounded_hours = round(cat_hours, 2)
        line_amount = round(rounded_hours * hourly_rate, 2)
        invoice_lines.append(
            InvoiceLineItem(
                description=f"Labor — {category} ({project_name})",
                quantity=rounded_hours,
                unit_price=hourly_rate,
                amount=line_amount,
                item_ref_id="1",
                item_ref_name="Irrigation Labor",
            )
        )

    for mat in aggregated_materials.values():
        qty = round(float(mat["quantity"]), 2)
        unit_price = round(float(mat["unit_price"]), 2)
        line_amount = round(qty * unit_price, 2)
        invoice_lines.append(
            InvoiceLineItem(
                description=f"Material — {mat['name']} (SKU: {mat['sku']})",
                quantity=qty,
                unit_price=unit_price,
                amount=line_amount,
                item_ref_id="1",
                item_ref_name=mat["name"],
                sku=mat["sku"],
            )
        )

    if not invoice_lines:
        raise ValueError(
            f"Unbilled shifts for project '{project_id}' contained 0 billable hours and 0 materials."
        )

    total_amount = round(sum(line.computed_amount for line in invoice_lines), 2)

    private_note_parts = [
        f"Project: {project_name} ({project_id})",
        f"Shifts Billed: {len(shift_ids)} ({total_hours:.2f} hrs)",
    ]
    if opts.private_note:
        private_note_parts.append(f"Manager Note: {opts.private_note}")
    if notes_summary:
        private_note_parts.append("Field Notes: " + " | ".join(notes_summary[:10]))
    combined_private_note = "\n".join(private_note_parts)[:4000]

    # 3. Call QuickBooksService.create_draft_invoice()
    qbo_line_payloads = [line.model_dump() for line in invoice_lines]
    qbo_invoice = await quickbooks_service.create_draft_invoice(
        client_id=qbo_customer_id,
        line_items=qbo_line_payloads,
        private_note=combined_private_note,
    )

    qbo_invoice_id = str(qbo_invoice.get("Id") or "")
    doc_number = qbo_invoice.get("DocNumber")
    qbo_total = float(qbo_invoice.get("TotalAmt", total_amount))

    # 4. Mark shifts as invoiced in Firestore & close project
    await firestore_service.mark_shifts_as_invoiced(
        shift_ids=shift_ids,
        invoice_id=qbo_invoice_id,
    )
    await firestore_service.update_project_status(
        project_id=project_id,
        status="closed",
        extra_fields={
            "last_qbo_invoice_id": qbo_invoice_id,
            "closed_at": datetime.now(timezone.utc),
        },
    )

    # 5. Notify manager via Telegram
    if opts.notify_manager:
        try:
            await telegram_service.notify_manager_draft_invoice(
                project_name=project_name,
                total_amount=qbo_total,
                qbo_invoice_id=qbo_invoice_id,
            )
        except TelegramNotificationError as exc:
            logger.warning(
                "Draft invoice '%s' created, but Telegram notification failed: %s",
                qbo_invoice_id,
                exc,
            )

    review_url = quickbooks_service.get_invoice_review_url(qbo_invoice_id)

    # 6. Return DraftInvoiceSummary
    return DraftInvoiceSummary(
        project_id=project_id,
        project_name=project_name,
        client_name=client_name,
        qbo_customer_id=qbo_customer_id,
        qbo_invoice_id=qbo_invoice_id,
        doc_number=str(doc_number) if doc_number is not None else None,
        status="DRAFT",
        email_status=str(qbo_invoice.get("EmailStatus", "NeedToSend")),
        total_amount=qbo_total,
        total_hours=round(total_hours, 2),
        shift_ids=shift_ids,
        line_items=invoice_lines,
        qbo_review_url=review_url,
    )


@router.post(
    "/{id}/close",
    response_model=DraftInvoiceSummary,
    status_code=status.HTTP_200_OK,
    summary="Close a project, create a QuickBooks draft invoice, and alert manager",
)
async def close_project(
    id: str = Path(..., description="Firestore project ID to close and bill"),
    body: ProjectCloseRequest | None = Body(default=None),
    _manager: UserContext = Depends(require_manager),
) -> DraftInvoiceSummary:
    """Manager-only endpoint that executes the end-of-project billing workflow:
    1. Aggregates all unbilled shifts from Firestore.
    2. Calculates labor lines and material lines.
    3. Creates a DRAFT invoice in QuickBooks Online (`EmailStatus="NeedToSend"`).
    4. Marks the shifts as invoiced with the generated QBO invoice ID and closes the project.
    5. Pings the manager via Telegram with a direct link to review the draft invoice.
    6. Returns the draft invoice summary.
    """
    try:
        return await execute_project_close_and_draft_invoice(
            project_id=id,
            close_options=body,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except QuickBooksAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": str(exc),
                "qbo_status_code": exc.status_code,
                "qbo_response": exc.response_body,
            },
        ) from exc
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
