import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import UserContext, get_current_user
from app.schemas.shift import ShiftLogCreate, ShiftLogResponse
from app.services.firestore_service import FirestoreServiceError, firestore_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shifts", tags=["Shifts"])


class ShiftSubmissionConfirmation(BaseModel):
    """Confirmation payload returned when an operator logs a shift."""

    message: str = Field(
        default="Shift logged successfully.",
        description="Status confirmation message",
    )
    shift: ShiftLogResponse = Field(..., description="Persisted shift log record")


@router.post(
    "",
    response_model=ShiftSubmissionConfirmation,
    status_code=status.HTTP_201_CREATED,
    summary="Submit an operator shift log",
)
async def create_shift(
    payload: ShiftLogCreate,
    current_user: UserContext = Depends(get_current_user),
) -> ShiftSubmissionConfirmation:
    """Record a field operator shift log in Firestore (`shifts` collection) with
    operator identity tracking (`operator_id`, `operator_name`, `operator_email`, `logged_by`).
    """
    # Operators can only submit shifts for themselves unless they are manager/admin
    effective_uid = (
        payload.operator_id
        if (current_user.is_manager_or_admin and payload.operator_id)
        else current_user.uid
    )
    effective_name = (
        payload.operator_name
        or current_user.name
        or (current_user.email.split("@")[0] if current_user.email else None)
        or effective_uid
    )
    effective_email = payload.operator_email or current_user.email
    effective_logged_by = (
        payload.logged_by
        or effective_name
        or effective_email
        or effective_uid
    )

    payload = payload.model_copy(
        update={
            "operator_id": effective_uid,
            "operator_name": effective_name,
            "operator_email": effective_email,
            "logged_by": effective_logged_by,
        }
    )

    try:
        await firestore_service.get_project(payload.project_id)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify project '{payload.project_id}': {exc}",
        ) from exc

    try:
        stored_shift = await firestore_service.create_shift_log(payload)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save shift log to Firestore: {exc}",
        ) from exc

    already_existed = bool(stored_shift.pop("_already_existed", False))
    stored_shift.pop("ute_id", None)

    if already_existed:
        return ShiftSubmissionConfirmation(
            message="Shift already synced (idempotent).",
            shift=ShiftLogResponse(**stored_shift),
        )

    return ShiftSubmissionConfirmation(
        message="Shift logged successfully.",
        shift=ShiftLogResponse(**stored_shift),
    )
