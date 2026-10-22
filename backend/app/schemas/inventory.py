from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.shift import ShiftItemUsed


class MaterialCatalogItem(BaseModel):
    """Catalog material item returned by material search and creation endpoints."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., min_length=1, description="Material document ID")
    item_id: str = Field(..., min_length=1, description="Unique inventory item ID")
    sku: str = Field(..., min_length=1, description="Part SKU")
    name: str = Field(..., min_length=1, description="Material name")
    unit: str = Field(default="Units", description="Unit of measure (e.g. Units, Meters)")
    unit_price: float = Field(default=0.0, ge=0, description="Standard unit price")


class MaterialCreateRequest(BaseModel):
    """Request payload for adding a new material to the Firebase catalog."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=200, description="Material name")
    unit: str = Field(default="Units", min_length=1, max_length=50, description="Unit of measure")
    sku: str | None = Field(default=None, description="Optional custom SKU")
    unit_price: float = Field(default=0.0, ge=0, description="Optional unit price")


class InventoryItem(BaseModel):
    """Master warehouse or catalog inventory item with reorder threshold."""

    model_config = ConfigDict(populate_by_name=True)

    item_id: str = Field(..., min_length=1, description="Unique inventory item ID")
    sku: str = Field(..., min_length=1, description="Part SKU")
    name: str = Field(..., min_length=1, description="Item name/description")
    unit: str = Field(default="ea", description="Unit of measure (e.g., ea, m, roll)")
    unit_price: float = Field(default=0.0, ge=0, description="Standard sell price per unit")
    qbo_item_id: str | None = Field(
        default=None, description="QuickBooks Online ItemRef ID"
    )
    quantity_on_hand: float = Field(
        default=0.0, description="Current total stock level"
    )
    reorder_threshold: float = Field(
        default=5.0, ge=0, description="Minimum stock level before triggering reorder alert"
    )
    reorder_quantity: float = Field(
        default=20.0, ge=0, description="Recommended order quantity when restocking"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp of last inventory update",
    )

    @property
    def is_below_threshold(self) -> bool:
        """Check whether current stock is at or below the reorder threshold."""
        return self.quantity_on_hand <= self.reorder_threshold


class UteStockItem(BaseModel):
    """Stock level for an item carried on a specific service Ute (`utes/{ute_id}/inventory/{item_id}`)."""

    model_config = ConfigDict(populate_by_name=True)

    ute_id: str = Field(..., min_length=1, description="Ute / vehicle identifier")
    item_id: str = Field(..., min_length=1, description="Inventory item ID")
    sku: str = Field(..., min_length=1, description="Part SKU")
    name: str = Field(..., min_length=1, description="Item name")
    quantity: float = Field(..., description="Current quantity available on this Ute")
    reorder_threshold: float = Field(
        default=2.0, ge=0, description="Minimum Ute stock threshold before restock alert"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last stock update timestamp",
    )

    @property
    def needs_restock(self) -> bool:
        return self.quantity <= self.reorder_threshold


class UteStockDeductionRequest(BaseModel):
    """Request payload for deducting consumed parts from a Ute's inventory."""

    ute_id: str = Field(..., min_length=1, description="Ute identifier")
    shift_id: str | None = Field(
        default=None, description="Associated shift ID that consumed the items"
    )
    items: list[ShiftItemUsed] = Field(
        ..., min_length=1, description="Items and quantities to deduct"
    )


class UteStockDeductionResult(BaseModel):
    """Result summary for a single item after a Ute stock deduction."""

    ute_id: str
    item_id: str
    sku: str
    name: str
    deducted_quantity: float
    previous_quantity: float
    remaining_quantity: float
    reorder_threshold: float
    below_threshold: bool


class ReorderThresholdUpdate(BaseModel):
    """Payload to update reorder threshold settings for an inventory item."""

    reorder_threshold: float = Field(..., ge=0, description="New minimum stock threshold")
    reorder_quantity: float | None = Field(
        default=None, ge=0, description="Optional updated recommended reorder quantity"
    )


class LowStockAlert(BaseModel):
    """Alert model emitted when warehouse or Ute stock drops at or below threshold."""

    location_type: str = Field(
        default="ute", description="'ute' or 'warehouse' stock location"
    )
    location_id: str = Field(..., description="Ute ID or warehouse identifier")
    item_id: str
    sku: str
    name: str
    current_quantity: float
    reorder_threshold: float
