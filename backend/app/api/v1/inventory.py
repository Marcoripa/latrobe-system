import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.core.security import UserContext, get_current_user, require_manager
from app.schemas.inventory import (
    LowStockAlert,
    MaterialCatalogItem,
    MaterialCreateRequest,
    UteStockDeductionRequest,
    UteStockDeductionResult,
    UteStockItem,
)
from app.services.firestore_service import FirestoreServiceError, firestore_service
from app.services.telegram_service import TelegramNotificationError, telegram_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inventory", tags=["Inventory"])


class UteInventoryResponse(BaseModel):
    """Inventory snapshot for a specific service Ute."""

    ute_id: str = Field(..., description="Ute / vehicle identifier")
    total_distinct_items: int = Field(
        ..., description="Total number of distinct SKUs tracked on this Ute"
    )
    low_stock_count: int = Field(
        ..., description="Number of items at or below reorder threshold"
    )
    items: list[UteStockItem] = Field(
        default_factory=list, description="Current stock items on this Ute"
    )
    low_stock_alerts: list[LowStockAlert] = Field(
        default_factory=list, description="Items currently needing restock"
    )


class LowStockAlertTriggerResponse(BaseModel):
    """Response returned when evaluating and dispatching low-stock alerts for a Ute."""

    ute_id: str
    alerts_found: int
    telegram_notified: bool
    alerts: list[LowStockAlert] = Field(default_factory=list)


@router.get(
    "/materials",
    response_model=list[MaterialCatalogItem],
    summary="Search materials in Firebase (supports text and vector search)",
)
async def search_materials(
    q: str = Query(
        default="",
        description="Search query string for material name or SKU (empty returns catalog)",
    ),
    limit: int = Query(default=30, ge=1, le=100),
    _current_user: UserContext = Depends(get_current_user),
) -> list[MaterialCatalogItem]:
    """Search for irrigation materials in Firebase (`materials` / `inventory` collections)."""
    try:
        results = await firestore_service.search_materials(query=q, limit=limit)
        return [MaterialCatalogItem(**item) for item in results]
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post(
    "/materials",
    response_model=MaterialCatalogItem,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new material in Firebase catalog",
)
async def create_material(
    payload: MaterialCreateRequest,
    _current_user: UserContext = Depends(get_current_user),
) -> MaterialCatalogItem:
    """Add a new material to the Firebase `materials` collection (ready for vectorization)."""
    try:
        created = await firestore_service.create_material(payload)
        return MaterialCatalogItem(**created)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get(
    "/utes/{ute_id}",
    response_model=UteInventoryResponse,
    summary="Inspect current stock levels on a specific Ute",
)
async def get_ute_stock(
    ute_id: str = Path(..., description="Ute vehicle identifier (e.g., 'ute-01')"),
    only_low_stock: bool = Query(
        default=False,
        description="If true, only return items at or below their reorder threshold",
    ),
    _current_user: UserContext = Depends(get_current_user),
) -> UteInventoryResponse:
    """Fetch all inventory items carried on `utes/{ute_id}/inventory` and highlight
    any items that have dropped at or below their reorder threshold.
    """
    try:
        raw_items = await firestore_service.get_ute_inventory(ute_id)
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    stock_items: list[UteStockItem] = []
    low_stock_alerts: list[LowStockAlert] = []

    for raw in raw_items:
        item = UteStockItem(
            ute_id=str(raw.get("ute_id") or ute_id),
            item_id=str(raw.get("item_id")),
            sku=str(raw.get("sku") or raw.get("item_id")),
            name=str(raw.get("name") or raw.get("item_id")),
            quantity=float(raw.get("quantity", 0.0)),
            reorder_threshold=float(raw.get("reorder_threshold", 2.0)),
        )
        if item.needs_restock:
            low_stock_alerts.append(
                LowStockAlert(
                    location_type="ute",
                    location_id=ute_id,
                    item_id=item.item_id,
                    sku=item.sku,
                    name=item.name,
                    current_quantity=item.quantity,
                    reorder_threshold=item.reorder_threshold,
                )
            )
        if not only_low_stock or item.needs_restock:
            stock_items.append(item)

    return UteInventoryResponse(
        ute_id=ute_id,
        total_distinct_items=len(raw_items),
        low_stock_count=len(low_stock_alerts),
        items=stock_items,
        low_stock_alerts=low_stock_alerts,
    )


@router.post(
    "/utes/{ute_id}/deduct",
    response_model=list[UteStockDeductionResult],
    summary="Manually deduct stock items from a Ute's inventory",
)
async def deduct_ute_inventory(
    ute_id: str = Path(..., description="Ute vehicle identifier"),
    request: UteStockDeductionRequest = ...,
    _current_user: UserContext = Depends(get_current_user),
) -> list[UteStockDeductionResult]:
    """Decrement stock for a list of items on a specific Ute and return updated balances."""
    try:
        results = await firestore_service.deduct_ute_stock(
            ute_id=ute_id,
            items=request.items,
        )
        return results
    except FirestoreServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post(
    "/utes/{ute_id}/alerts",
    response_model=LowStockAlertTriggerResponse,
    summary="Check Ute inventory and trigger Telegram low-stock alerts to manager",
)
async def trigger_ute_low_stock_alert(
    ute_id: str = Path(..., description="Ute vehicle identifier"),
    _manager: UserContext = Depends(require_manager),
) -> LowStockAlertTriggerResponse:
    """Inspect a Ute's inventory for any items at or below reorder threshold and send
    an immediate Telegram alert to the operations manager.
    """
    snapshot = await get_ute_stock(
        ute_id=ute_id,
        only_low_stock=False,
        _current_user=_manager,
    )

    telegram_notified = False
    if snapshot.low_stock_alerts:
        alert_dicts: list[dict[str, Any]] = [
            alert.model_dump() for alert in snapshot.low_stock_alerts
        ]
        try:
            tg_res = await telegram_service.notify_low_stock_alerts(
                ute_id=ute_id,
                alerts=alert_dicts,
            )
            telegram_notified = bool(tg_res.get("ok")) and not bool(
                tg_res.get("skipped")
            )
        except TelegramNotificationError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Detected {len(snapshot.low_stock_alerts)} low-stock items, but Telegram alert failed: {exc}",
            ) from exc

    return LowStockAlertTriggerResponse(
        ute_id=ute_id,
        alerts_found=len(snapshot.low_stock_alerts),
        telegram_notified=telegram_notified,
        alerts=snapshot.low_stock_alerts,
    )
