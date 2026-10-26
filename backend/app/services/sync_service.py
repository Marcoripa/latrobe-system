import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.services.firestore_service import FirestoreService, firestore_service
from app.services.geocoding_service import format_display_address, geocode_address_dict
from app.services.quickbooks_service import QuickBooksService, quickbooks_service

logger = logging.getLogger(__name__)

# Firestore allows at most 500 operations per batch; keep a safety margin.
BATCH_CHUNK_SIZE = 450


class SyncService:
    """Mirrors QuickBooks Online (single source of truth) into Cloud Firestore.

    - QBO Customers (no ParentRef)  -> `customers/{qbo_id}`
    - QBO Sub-Customers / Jobs      -> `projects/{qbo_id}`
    - QBO Items                     -> `catalog_items/{qbo_id}`

    The QBO `Id` is always the Firestore document ID, so repeated runs are idempotent upserts.
    """

    def __init__(
        self,
        qbo: QuickBooksService | None = None,
        fs: FirestoreService | None = None,
    ) -> None:
        self._qbo = qbo or quickbooks_service
        self._fs = fs or firestore_service
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def _batch_upsert(
        self,
        collection: str,
        documents: list[tuple[str, dict[str, Any]]],
    ) -> int:
        """Upsert `(doc_id, data)` pairs using chunked Firestore batch writes (merge=True)."""
        if not documents:
            return 0

        def _write() -> int:
            col = self._fs.db.collection(collection)
            written = 0
            for idx in range(0, len(documents), BATCH_CHUNK_SIZE):
                chunk = documents[idx : idx + BATCH_CHUNK_SIZE]
                batch = self._fs.db.batch()
                for doc_id, data in chunk:
                    batch.set(col.document(doc_id), data, merge=True)
                batch.commit()
                written += len(chunk)
            return written

        return await asyncio.to_thread(_write)

    async def _existing_ids(self, collection: str) -> set[str]:
        """Return the set of document IDs already present in a collection (no field reads)."""

        def _read() -> set[str]:
            return {
                doc.id
                for doc in self._fs.db.collection(collection).select([]).stream()
            }

        return await asyncio.to_thread(_read)

    async def _existing_projects_geo(self) -> dict[str, dict[str, Any]]:
        """Return cached lat/lng/address mapping for projects to avoid redundant geocoding."""

        def _read() -> dict[str, dict[str, Any]]:
            cached: dict[str, dict[str, Any]] = {}
            for doc in self._fs.db.collection("projects").stream():
                data = doc.to_dict() or {}
                cached[doc.id] = {
                    "lat": data.get("lat"),
                    "lng": data.get("lng"),
                    "address": data.get("address") or data.get("location"),
                }
            return cached

        return await asyncio.to_thread(_read)

    async def sync_qbo_customers_and_projects(self) -> dict[str, int]:
        """Fetch active QBO customers and upsert them into `customers` and `projects`.
        Automatically geocodes QuickBooks Shipping Addresses into `lat` and `lng` for projects.
        """
        records = await self._qbo.fetch_active_customers()
        now = datetime.now(timezone.utc)

        customer_docs: list[tuple[str, dict[str, Any]]] = []
        project_records: list[dict[str, Any]] = []
        customer_addrs: dict[str, dict[str, Any]] = {}

        for rec in records:
            primary_addr = rec.get("ship_addr") or rec.get("bill_addr")
            if primary_addr:
                customer_addrs[rec["qbo_id"]] = primary_addr

            if rec["is_project"] and rec.get("parent_ref"):
                project_records.append(rec)
            else:
                cust_data: dict[str, Any] = {
                    "qbo_id": rec["qbo_id"],
                    "qbo_customer_id": rec["qbo_id"],
                    "display_name": rec["display_name"],
                    "name": rec["display_name"],
                    "fully_qualified_name": rec.get("fully_qualified_name"),
                    "company_name": rec.get("company_name"),
                    "active": True,
                    "synced_at": now,
                    "source": "quickbooks",
                }
                if primary_addr:
                    formatted_addr = format_display_address(primary_addr)
                    if formatted_addr:
                        cust_data["address"] = formatted_addr
                        cust_data["location"] = formatted_addr

                customer_docs.append((rec["qbo_id"], cust_data))

        customer_names = {doc_id: data["display_name"] for doc_id, data in customer_docs}
        existing_project_ids = await self._existing_ids("projects")
        existing_geo = await self._existing_projects_geo()

        project_docs: list[tuple[str, dict[str, Any]]] = []
        for rec in project_records:
            parent_id = rec["parent_ref"]
            client_name = rec.get("parent_name") or customer_names.get(parent_id) or ""

            # Check project's own ship/bill address, fallback to parent client address
            target_addr = (
                rec.get("ship_addr")
                or rec.get("bill_addr")
                or customer_addrs.get(parent_id)
            )
            display_addr = format_display_address(target_addr)

            # Check if we already have valid cached coordinates for this project
            cached = existing_geo.get(rec["qbo_id"])
            lat: float | None = None
            lng: float | None = None

            if (
                cached
                and cached.get("lat") is not None
                and cached.get("lng") is not None
                and cached.get("address") == display_addr
            ):
                lat = float(cached["lat"])
                lng = float(cached["lng"])
            elif target_addr:
                lat, lng, _ = await geocode_address_dict(target_addr)
                # Respect Nominatim rate limit
                await asyncio.sleep(0.5)

            data: dict[str, Any] = {
                "qbo_id": rec["qbo_id"],
                "qbo_customer_id": rec["qbo_id"],
                "parent_customer_id": parent_id,
                "client_id": parent_id,
                "client_name": client_name,
                "name": rec["display_name"],
                "display_name": rec["display_name"],
                "synced_at": now,
                "source": "quickbooks",
            }

            if display_addr:
                data["address"] = display_addr
                data["location"] = display_addr

            if lat is not None and lng is not None:
                data["lat"] = lat
                data["lng"] = lng

            # Only initialise operational fields for brand-new projects so the sync never
            # reopens projects that were closed/billed locally.
            if rec["qbo_id"] not in existing_project_ids:
                data.update(
                    {
                        "status": "active",
                        "hourly_rate": 125.0,
                        "created_at": now,
                    }
                )
            project_docs.append((rec["qbo_id"], data))

        customers_written = await self._batch_upsert("customers", customer_docs)
        projects_written = await self._batch_upsert("projects", project_docs)

        logger.info(
            "QBO sync: upserted %d customer(s) and %d project(s).",
            customers_written,
            projects_written,
        )
        return {"customers": customers_written, "projects": projects_written}

    async def sync_qbo_items(self) -> dict[str, int]:
        """Fetch active QBO items and upsert them into `catalog_items`."""
        items = await self._qbo.fetch_active_items()
        now = datetime.now(timezone.utc)

        docs: list[tuple[str, dict[str, Any]]] = []
        for item in items:
            docs.append(
                (
                    item["qbo_id"],
                    {
                        "id": item["qbo_id"],
                        "item_id": item["qbo_id"],
                        "qbo_id": item["qbo_id"],
                        "qbo_item_id": item["qbo_id"],
                        "sku": item["sku"],
                        "name": item["name"],
                        "description": item["description"],
                        "unit_price": item["unit_price"],
                        "type": item.get("type"),
                        "unit": "Units",
                        "active": True,
                        "synced_at": now,
                        "source": "quickbooks",
                    },
                )
            )

        written = await self._batch_upsert("catalog_items", docs)
        logger.info("QBO sync: upserted %d catalog item(s).", written)
        return {"catalog_items": written}

    async def run_full_sync(self) -> dict[str, Any]:
        """Run customers/projects then items sequentially. Skips if a sync is already running."""
        if self._lock.locked():
            logger.info("QBO sync already in progress; skipping this trigger.")
            return {"skipped": True}

        async with self._lock:
            summary: dict[str, Any] = {}
            try:
                summary.update(await self.sync_qbo_customers_and_projects())
            except Exception:
                logger.exception("QBO customers/projects sync failed.")
                summary["customers_projects_error"] = True
            try:
                summary.update(await self.sync_qbo_items())
            except Exception:
                logger.exception("QBO items sync failed.")
                summary["items_error"] = True
            return summary


sync_service = SyncService()
