import logging
import secrets
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import get_current_user
from app.services.sync_service import sync_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["Sync"])


class SyncResponse(BaseModel):
    message: str
    already_running: bool = False
    summary: dict[str, Any] | None = None


@router.post(
    "/qbo-to-firestore",
    response_model=SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger QuickBooks Online -> Firestore sync (Admin or Secret Token)",
)
async def trigger_qbo_sync(
    request: Request,
    background_tasks: BackgroundTasks,
    secret: str | None = Query(None, description="Secret token for server-to-server or Cloud Scheduler calls"),
    x_sync_secret: str | None = Header(None, alias="X-Sync-Secret"),
    wait: bool = Query(False, description="If true, waits for sync completion and returns summary directly"),
) -> SyncResponse:
    """Queue or run a full sync (customers/projects, then items).

    Accepts either:
    1. A secret token (`?secret=...` or `X-Sync-Secret: ...`) matching `TELEGRAM_WEBHOOK_SECRET`, OR
    2. A valid Firebase Admin ID token in the `Authorization: Bearer <token>` header.
    """
    # 1. Check secret token (for Cloud Scheduler, scripts, and easy curl testing)
    provided_secret = secret or x_sync_secret
    authorized_by_secret = False

    if provided_secret and settings.TELEGRAM_WEBHOOK_SECRET:
        if secrets.compare_digest(provided_secret, settings.TELEGRAM_WEBHOOK_SECRET):
            authorized_by_secret = True

    # 2. If not authorized by secret, check Firebase Auth Bearer token
    if not authorized_by_secret:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Provide a valid Bearer token or ?secret= parameter.",
            )
        token = auth_header[7:].strip()
        from fastapi.security import HTTPAuthorizationCredentials
        user = await get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        if not user.is_manager_or_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin or manager privileges required.",
            )

    # 3. Handle concurrent executions
    if sync_service.is_running:
        return SyncResponse(
            message="A QuickBooks sync is already in progress.",
            already_running=True,
        )

    # 4. Execute sync
    if wait:
        summary = await sync_service.run_full_sync()
        return SyncResponse(
            message="QuickBooks sync completed successfully.",
            already_running=False,
            summary=summary,
        )

    background_tasks.add_task(sync_service.run_full_sync)
    return SyncResponse(
        message="QuickBooks sync started in the background.",
        already_running=False,
    )
