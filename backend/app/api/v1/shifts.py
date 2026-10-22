import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import UserContext, get_current_user
from app.schemas.inventory import UteStockDeductionResult
from app.schemas.shift import ShiftLogCreate, ShiftLogResponse
from app.services.firestore_service import FirestoreServiceError, firestore_service
from app.services.telegram_service import TelegramNotificationError, telegram_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shifts", tags=["Shifts"])


class ShiftSubmissionConfirmation(BaseModel):
    """Confirmation payload returned when an operator logs a shift."""

    message: str = Field(
        default="Shift logged and Ute stock updated.",
        description="Status confirmation message",
    )
    shift: ShiftLogResponse = Field(..., description="Persisted shift log record")
    ute_id: str | None = Field(
        default=None, description="Ute ID from which consumed stock was deducted"
    )
    stock_deductions: list[UteStockDeductionResult] = Field(
        default_factory=list,
        description="Per-item stock deduction results from the operator's Ute",
    )


@router.post(
    "",
    response_model=ShiftSubmissionConfirmation,
    status_code=status.HTTP_201_CREATED,
    summary="Submit an operator shift log and deduct consumed Ute stock",
)
async def create_shift(
    payload: ShiftLogCreate,
    current_user: UserContext = Depends(get_current_user),
) -> ShiftSubmissionConfirmation:
    """Record a field operator shift log in Firestore, automatically deduct any consumed
    materials from the assigned Ute's inventory (`utes/{ute_id}/inventory/{item_id}`),
    and dispatch low-stock Telegram alerts if any item falls at or below its reorder threshold.
    """
    # Operators can only submit shifts for themselves unless they are manager/admin
    if not current_user.is_manager_or_admin and payload.operator_id != current_user.uid:
        payload = payload.model_copy(update={"operator_id": current_user.uid})

    try:
        project_doc: dict[str, Any] | None = await firestore_service.get_project(
            payload.project_id
        )
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify project '{payload.project_id}': {exc}",
        ) from exc

    # Resolve target Ute ID from payload override, project assignment, operator claims, or default 'ute-01'
    target_ute_id = (
        payload.ute_id
        or (
            (project_doc.get("ute_id") or project_doc.get("uteId"))
            if project_doc
            else None
        )
        or current_user.claims.get("ute_id")
        or ("ute-01" if payload.items_used else None)
    )

    if target_ute_id and payload.ute_id != target_ute_id:
        payload = payload.model_copy(update={"ute_id": target_ute_id})

    try:
        stored_shift = await firestore_service.create_shift_log(payload)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save shift log to Firestore: {exc}",
        ) from exc

    already_existed = bool(stored_shift.pop("_already_existed", False))
    if already_existed:
        return ShiftSubmissionConfirmation(
            message="Shift already synced (idempotent).",
            shift=ShiftLogResponse(**stored_shift),
            ute_id=target_ute_id,
            stock_deductions=[],
        )

    stock_deductions: list[UteStockDeductionResult] = []
    if payload.items_used and target_ute_id:
        try:
            stock_deductions = await firestore_service.deduct_ute_stock(
                ute_id=target_ute_id,
                items=payload.items_used,
            )
        except FirestoreServiceError as exc:
            logger.error(
                "Shift '%s' saved, but Ute stock deduction failed on '%s': %s",
                stored_shift.get("id"),
                target_ute_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Shift '{stored_shift.get('id')}' was saved, but deducting stock "
                    f"from Ute '{target_ute_id}' failed: {exc}"
                ),
            ) from exc

        low_stock_items = [
            {
                "item_id": item.item_id,
                "sku": item.sku,
                "name": item.name,
                "current_quantity": item.remaining_quantity,
                "reorder_threshold": item.reorder_threshold,
            }
            for item in stock_deductions
            if item.below_threshold
        ]
        if low_stock_items:
            try:
                await telegram_service.notify_low_stock_alerts(
                    ute_id=target_ute_id,
                    alerts=low_stock_items,
                )
            except TelegramNotificationError as exc:
                logger.warning(
                    "Non-fatal: failed to send Telegram low-stock alert for Ute '%s': %s",
                    target_ute_id,
                    exc,
                )

    return ShiftSubmissionConfirmation(
        message="Shift logged and Ute stock updated successfully.",
        shift=ShiftLogResponse(**stored_shift),
        ute_id=target_ute_id,
        stock_deductions=stock_deductions,
    )
