import json
import logging
from collections import defaultdict
from typing import Any

from app.api.v1.projects import ProjectCloseRequest, execute_project_close_and_draft_invoice
from app.services.firestore_service import FirestoreServiceError, firestore_service
from app.services.quickbooks_service import QuickBooksAPIError

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - fallback to standalone fastmcp package
    from fastmcp import FastMCP  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "LaTrobe Irrigation Operations Brain",
    dependencies=["firebase-admin", "httpx", "pydantic"],
)


def _validate_identifier(value: str, field_name: str) -> str:
    """Validate and strip a required non-empty identifier string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field_name}' must be a non-empty string.")
    cleaned = value.strip()
    if "/" in cleaned:
        raise ValueError(f"'{field_name}' must not contain path separators ('/').")
    return cleaned


@mcp.tool()
async def get_project_work_summary(project_id: str) -> str:
    """Summarize total labor hours (broken down by category and billing status),
    all inventory parts consumed, and chronological field notes to date for a project.

    Args:
        project_id: The Firestore document ID of the project (e.g., "proj_101").

    Returns:
        A JSON-formatted string containing project metadata, hours summary,
        aggregated parts used, and operator field notes.
    """
    try:
        clean_project_id = _validate_identifier(project_id, "project_id")
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, indent=2)

    try:
        project_doc = await firestore_service.get_project(clean_project_id)
        shifts = await firestore_service.get_project_shifts(
            clean_project_id, only_unbilled=False
        )
    except FirestoreServiceError as exc:
        logger.error("MCP get_project_work_summary failed for '%s': %s", project_id, exc)
        return json.dumps(
            {"ok": False, "project_id": clean_project_id, "error": str(exc)},
            indent=2,
        )

    if not project_doc and not shifts:
        return json.dumps(
            {
                "ok": False,
                "project_id": clean_project_id,
                "error": f"Project '{clean_project_id}' not found and has no recorded shifts.",
            },
            indent=2,
        )

    total_hours = 0.0
    unbilled_hours = 0.0
    billed_hours = 0.0
    hours_by_category: dict[str, float] = defaultdict(float)
    parts_used: dict[str, dict[str, Any]] = {}
    field_notes: list[dict[str, Any]] = []

    for shift in shifts:
        hrs = float(shift.get("hours") or 0.0)
        cat = str(shift.get("category") or "On-Site")
        is_invoiced = bool(shift.get("invoiced") is True)

        total_hours += hrs
        hours_by_category[cat] = round(hours_by_category[cat] + hrs, 2)
        if is_invoiced:
            billed_hours += hrs
        else:
            unbilled_hours += hrs

        for item in shift.get("items_used") or []:
            sku = str(item.get("sku") or item.get("item_id") or "UNKNOWN")
            qty = float(item.get("quantity") or 0.0)
            if sku not in parts_used:
                parts_used[sku] = {
                    "item_id": item.get("item_id"),
                    "sku": sku,
                    "name": item.get("name", sku),
                    "total_quantity": 0.0,
                }
            parts_used[sku]["total_quantity"] = round(
                parts_used[sku]["total_quantity"] + qty, 2
            )

        note_text = shift.get("notes")
        if note_text:
            created_at = shift.get("created_at")
            created_str = (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at or "")
            )
            field_notes.append(
                {
                    "shift_id": shift.get("id"),
                    "operator_id": shift.get("operator_id"),
                    "category": cat,
                    "hours": hrs,
                    "invoiced": is_invoiced,
                    "notes": note_text,
                    "photo_url": shift.get("photo_url"),
                    "created_at": created_str,
                }
            )

    summary_payload = {
        "ok": True,
        "project_id": clean_project_id,
        "project_name": (project_doc or {}).get("name", clean_project_id),
        "client_name": (project_doc or {}).get("client_name", "Unknown Client"),
        "status": (project_doc or {}).get("status", "unknown"),
        "assigned_ute_id": (project_doc or {}).get("ute_id"),
        "shift_counts": {
            "total_shifts": len(shifts),
            "unbilled_shifts": sum(1 for s in shifts if not s.get("invoiced")),
            "invoiced_shifts": sum(1 for s in shifts if s.get("invoiced")),
        },
        "hours_summary": {
            "total_hours": round(total_hours, 2),
            "unbilled_hours": round(unbilled_hours, 2),
            "billed_hours": round(billed_hours, 2),
            "by_category": dict(hours_by_category),
        },
        "parts_used": list(parts_used.values()),
        "field_notes": field_notes,
    }
    return json.dumps(summary_payload, indent=2)


@mcp.tool()
async def trigger_draft_invoice(project_id: str) -> str:
    """Trigger the end-of-project billing workflow for a given project.

    Aggregates all unbilled shifts from Firestore, calculates labor and material line items,
    creates a DRAFT invoice in QuickBooks Online (`EmailStatus="NeedToSend"` so the customer
    is never auto-emailed), marks the shifts as invoiced, and sends a Telegram review alert
    to the manager.

    Args:
        project_id: The Firestore document ID of the project to bill (e.g., "proj_101").

    Returns:
        A JSON-formatted string with the created QuickBooks draft invoice ID, total amount,
        billed shift IDs, line items, and manager review URL.
    """
    try:
        clean_project_id = _validate_identifier(project_id, "project_id")
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, indent=2)

    try:
        draft_summary = await execute_project_close_and_draft_invoice(
            project_id=clean_project_id,
            close_options=ProjectCloseRequest(notify_manager=True),
        )
        return json.dumps(
            {
                "ok": True,
                "message": (
                    f"Draft invoice {draft_summary.qbo_invoice_id} created in QuickBooks Online "
                    f"for '{draft_summary.project_name}' (${draft_summary.total_amount:,.2f} AUD)."
                ),
                "invoice": draft_summary.model_dump(mode="json"),
            },
            indent=2,
        )
    except (LookupError, ValueError, QuickBooksAPIError, FirestoreServiceError) as exc:
        logger.error(
            "MCP trigger_draft_invoice failed for project '%s': %s",
            clean_project_id,
            exc,
        )
        return json.dumps(
            {
                "ok": False,
                "project_id": clean_project_id,
                "error": str(exc),
            },
            indent=2,
        )


@mcp.tool()
async def check_ute_inventory(ute_id: str) -> str:
    """Inspect current parts and stock counts on a specific service Ute vehicle,
    highlighting any items that are at or below their reorder threshold.

    Args:
        ute_id: The identifier of the work vehicle / Ute (e.g., "ute-01").

    Returns:
        A JSON-formatted string containing all stock items on the vehicle and any
        active low-stock reorder alerts.
    """
    try:
        clean_ute_id = _validate_identifier(ute_id, "ute_id")
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, indent=2)

    try:
        raw_items = await firestore_service.get_ute_inventory(clean_ute_id)
    except FirestoreServiceError as exc:
        logger.error("MCP check_ute_inventory failed for '%s': %s", clean_ute_id, exc)
        return json.dumps(
            {"ok": False, "ute_id": clean_ute_id, "error": str(exc)},
            indent=2,
        )

    formatted_items: list[dict[str, Any]] = []
    low_stock_items: list[dict[str, Any]] = []

    for item in raw_items:
        qty = float(item.get("quantity", 0.0))
        threshold = float(item.get("reorder_threshold", 2.0))
        needs_restock = qty <= threshold
        entry = {
            "item_id": item.get("item_id"),
            "sku": item.get("sku"),
            "name": item.get("name"),
            "quantity": qty,
            "reorder_threshold": threshold,
            "needs_restock": needs_restock,
        }
        formatted_items.append(entry)
        if needs_restock:
            low_stock_items.append(entry)

    return json.dumps(
        {
            "ok": True,
            "ute_id": clean_ute_id,
            "total_skus": len(formatted_items),
            "low_stock_count": len(low_stock_items),
            "low_stock_items": low_stock_items,
            "inventory": formatted_items,
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
