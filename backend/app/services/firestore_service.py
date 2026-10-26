import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.core.security import initialize_firebase
from app.schemas.inventory import MaterialCreateRequest, UteStockDeductionResult
from app.schemas.project import ProjectCreate
from app.schemas.shift import ShiftItemUsed, ShiftLogCreate

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_MATERIALS: list[dict[str, Any]] = [
    {"id": "19mm-pipe", "item_id": "19mm-pipe", "sku": "19MM-PIPE", "name": "19mm Pipe", "unit": "Meters", "unit_price": 4.5},
    {"id": "25mm-solenoid", "item_id": "25mm-solenoid", "sku": "25MM-SOLENOID", "name": "25mm Solenoid", "unit": "Units", "unit_price": 65.0},
    {"id": "25mm-elbow", "item_id": "25mm-elbow", "sku": "25MM-ELBOW", "name": "25mm Elbow", "unit": "Units", "unit_price": 3.5},
]



class FirestoreServiceError(Exception):
    """Raised when a Firestore operation fails."""


class FirestoreService:
    """Asynchronous wrapper around the Firebase Admin Firestore client."""

    def __init__(self) -> None:
        self._db: firestore.Client | None = None

    @property
    def db(self) -> firestore.Client:
        """Lazily initialize and return the Firestore client."""
        if self._db is None:
            initialize_firebase()
            try:
                self._db = firestore.client()
            except Exception as exc:
                logger.error("Failed to obtain Firestore client: %s", exc)
                raise FirestoreServiceError(
                    f"Firestore client initialization failed: {exc}"
                ) from exc
        return self._db

    async def create_project(self, project: ProjectCreate) -> dict[str, Any]:
        """Create a new project document under the `projects` collection."""

        def _create() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            payload = project.model_dump()
            payload.update(
                {
                    "created_at": now,
                    "updated_at": now,
                }
            )
            doc_ref = self.db.collection("projects").document()
            doc_ref.set(payload)
            payload["id"] = doc_ref.id
            return payload

        try:
            return await asyncio.to_thread(_create)
        except Exception as exc:
            logger.error("Error creating project '%s': %s", project.name, exc)
            raise FirestoreServiceError(
                f"Failed to create project '{project.name}': {exc}"
            ) from exc

    async def list_projects(
        self,
        status: str | None = None,
        *,
        status_filter: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch projects from `projects` collection, optionally filtered by status and limit."""
        effective_status = status if status is not None else status_filter

        def _fetch_all() -> list[dict[str, Any]]:
            col_ref = self.db.collection("projects")
            query: Any = (
                col_ref.where(filter=FieldFilter("status", "==", effective_status))
                if effective_status
                else col_ref
            )
            if limit is not None:
                query = query.limit(limit)
            projects: list[dict[str, Any]] = []
            for doc in query.stream():
                data = doc.to_dict() or {}
                data["id"] = doc.id
                projects.append(data)
            return projects

        try:
            return await asyncio.to_thread(_fetch_all)
        except Exception as exc:
            logger.error(
                "Error listing projects (status=%s): %s", effective_status, exc
            )
            raise FirestoreServiceError(
                f"Failed to list projects: {exc}"
            ) from exc

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        """Fetch a project document by ID from `projects/{project_id}`."""

        def _fetch() -> dict[str, Any] | None:
            doc_ref = self.db.collection("projects").document(project_id)
            snapshot = doc_ref.get()
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}
            data["id"] = snapshot.id
            return data

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.error("Error fetching project '%s': %s", project_id, exc)
            raise FirestoreServiceError(
                f"Failed to fetch project '{project_id}': {exc}"
            ) from exc

    async def create_shift_log(self, shift: ShiftLogCreate) -> dict[str, Any]:
        """Persist a new shift log under `shifts` collection (idempotent when client_shift_id is set)."""

        def _create() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            shifts_col = self.db.collection("shifts")
            if shift.client_shift_id:
                doc_ref = shifts_col.document(shift.client_shift_id)
                existing_snap = doc_ref.get()
                if existing_snap.exists:
                    existing_data = existing_snap.to_dict() or {}
                    existing_data["id"] = existing_snap.id
                    existing_data["_already_existed"] = True
                    return existing_data
            else:
                doc_ref = shifts_col.document()

            payload = shift.model_dump()
            payload.update(
                {
                    "invoiced": False,
                    "invoice_id": None,
                    "invoiced_at": None,
                    "created_at": now,
                }
            )
            doc_ref.set(payload)
            payload["id"] = doc_ref.id
            return payload

        try:
            return await asyncio.to_thread(_create)
        except Exception as exc:
            logger.error(
                "Error creating shift log for project '%s': %s", shift.project_id, exc
            )
            raise FirestoreServiceError(f"Failed to create shift log: {exc}") from exc

    async def get_project_shifts(
        self, project_id: str, *, only_unbilled: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch shifts for a project, optionally filtering to only unbilled shifts."""

        def _query() -> list[dict[str, Any]]:
            shifts_ref = self.db.collection("shifts")
            query = shifts_ref.where(
                filter=FieldFilter("project_id", "==", project_id)
            )
            results: list[dict[str, Any]] = []
            for doc in query.stream():
                data = doc.to_dict() or {}
                if only_unbilled and data.get("invoiced") is True:
                    continue
                data["id"] = doc.id
                results.append(data)
            return results

        try:
            shifts = await asyncio.to_thread(_query)
            return shifts
        except Exception as exc:
            logger.error(
                "Error querying shifts for project '%s': %s", project_id, exc
            )
            raise FirestoreServiceError(
                f"Failed to fetch shifts for project '{project_id}': {exc}"
            ) from exc

    async def get_unbilled_shifts(self, project_id: str) -> list[dict[str, Any]]:
        """Fetch all shifts for a project that have not been marked `invoiced=True`."""
        shifts = await self.get_project_shifts(project_id, only_unbilled=True)
        logger.info(
            "Retrieved %d unbilled shift(s) for project '%s'",
            len(shifts),
            project_id,
        )
        return shifts

    async def update_project_status(
        self,
        project_id: str,
        status: str,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update the lifecycle status of a project in `projects/{project_id}`."""

        def _update() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            doc_ref = self.db.collection("projects").document(project_id)
            updates: dict[str, Any] = {
                "status": status,
                "updated_at": now,
            }
            if extra_fields:
                updates.update(extra_fields)
            doc_ref.set(updates, merge=True)
            snapshot = doc_ref.get()
            data = snapshot.to_dict() or {}
            data["id"] = snapshot.id
            return data

        try:
            return await asyncio.to_thread(_update)
        except Exception as exc:
            logger.error(
                "Error updating status for project '%s': %s", project_id, exc
            )
            raise FirestoreServiceError(
                f"Failed to update project '{project_id}': {exc}"
            ) from exc

    async def find_project_by_name_or_id(
        self, identifier: str
    ) -> dict[str, Any] | None:
        """Resolve a project by exact Firestore document ID or case-insensitive name/client match."""
        clean_id = identifier.strip()
        if not clean_id:
            return None

        # 1. Try direct document ID lookup first
        direct = await self.get_project(clean_id)
        if direct:
            return direct

        # 2. Fallback to case-insensitive search across projects collection
        def _search_projects() -> dict[str, Any] | None:
            needle = clean_id.lower()
            # Strip trailing words like "project" or "job" for cleaner matching
            normalized_needle = (
                needle.replace("project", "").replace("job", "").strip() or needle
            )
            projects_ref = self.db.collection("projects")
            partial_match: dict[str, Any] | None = None

            for doc in projects_ref.stream():
                data = doc.to_dict() or {}
                data["id"] = doc.id
                doc_name = str(data.get("name") or "").lower()
                client_name = str(data.get("client_name") or "").lower()
                doc_id_lower = doc.id.lower()

                if needle in (doc_name, client_name, doc_id_lower) or (
                    normalized_needle and normalized_needle == doc_name
                ):
                    return data

                if (
                    normalized_needle
                    and (
                        normalized_needle in doc_name
                        or normalized_needle in client_name
                        or doc_name in normalized_needle
                    )
                    and partial_match is None
                ):
                    partial_match = data

            return partial_match

        try:
            return await asyncio.to_thread(_search_projects)
        except Exception as exc:
            logger.error("Error searching project for '%s': %s", identifier, exc)
            raise FirestoreServiceError(
                f"Failed to search project '{identifier}': {exc}"
            ) from exc

    async def get_project_status(self, project_identifier: str) -> dict[str, Any]:
        """Fetch project details, client info, and total / unbilled logged hours."""
        project = await self.find_project_by_name_or_id(project_identifier)
        if not project:
            return {"found": False, "error": f"Project '{project_identifier}' not found."}

        project_id = str(project.get("id"))
        shifts = await self.get_project_shifts(project_id, only_unbilled=False)
        total_hours = sum(float(s.get("hours") or 0.0) for s in shifts)
        unbilled_shifts = [s for s in shifts if not s.get("invoiced")]
        unbilled_hours = sum(float(s.get("hours") or 0.0) for s in unbilled_shifts)

        return {
            "found": True,
            "project_id": project_id,
            "name": str(project.get("name") or project_id),
            "client_name": str(project.get("client_name") or "N/A"),
            "status": str(project.get("status") or "active"),
            "total_hours": total_hours,
            "shifts_count": len(shifts),
            "unbilled_hours": unbilled_hours,
            "unbilled_shifts_count": len(unbilled_shifts),
        }

    async def list_active_projects(self) -> dict[str, Any]:
        """Retrieve all currently active projects."""
        projects = await self.list_projects(status_filter="active", limit=20)
        return {
            "projects": [
                {
                    "id": str(p.get("id")),
                    "name": str(p.get("name") or p.get("id")),
                    "client_name": str(p.get("client_name") or "Unknown Client"),
                    "status": str(p.get("status") or "active"),
                }
                for p in projects
            ]
        }

    async def get_ute_inventory(self, ute_id: str) -> list[dict[str, Any]]:
        """Fetch all inventory items for a specific Ute under `utes/{ute_id}/inventory`."""

        def _fetch_inventory() -> list[dict[str, Any]]:
            inv_ref = (
                self.db.collection("utes").document(ute_id).collection("inventory")
            )
            items: list[dict[str, Any]] = []
            for doc in inv_ref.stream():
                data = doc.to_dict() or {}
                data.setdefault("item_id", doc.id)
                data.setdefault("ute_id", ute_id)
                data.setdefault("sku", doc.id)
                data.setdefault("name", doc.id)
                data.setdefault("quantity", 0.0)
                data.setdefault("reorder_threshold", 2.0)
                items.append(data)
            return items

        try:
            return await asyncio.to_thread(_fetch_inventory)
        except Exception as exc:
            logger.error("Error fetching inventory for Ute '%s': %s", ute_id, exc)
            raise FirestoreServiceError(
                f"Failed to fetch inventory for Ute '{ute_id}': {exc}"
            ) from exc

    async def find_ute_inventory_by_identifier(
        self, identifier: str
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Resolve a Ute by document ID, operator name, or vehicle label (e.g., "John's ute", "ute-01")
        and return `(resolved_ute_id, display_name, inventory_items)`.
        """
        raw = identifier.strip()
        # Normalize possessive phrasing like "John's ute" -> "john"
        cleaned = (
            raw.lower()
            .replace("'s", "")
            .replace("’s", "")
            .replace("ute", "")
            .replace("truck", "")
            .replace("vehicle", "")
            .strip()
        )
        candidate_id = cleaned or raw

        # 1. Try direct lookup on raw and normalized slug
        for direct_id in (raw, raw.lower().replace(" ", "-"), candidate_id):
            if not direct_id or "/" in direct_id:
                continue
            items = await self.get_ute_inventory(direct_id)
            if items:
                return direct_id, raw, items

        # 2. Search the `utes` collection for matching `name`, `operator_name`, or document ID
        def _resolve_ute_doc() -> tuple[str, str] | None:
            utes_ref = self.db.collection("utes")
            for doc in utes_ref.stream():
                data = doc.to_dict() or {}
                doc_id = doc.id
                label = str(
                    data.get("name")
                    or data.get("operator_name")
                    or data.get("driver")
                    or doc_id
                )
                haystack = " ".join(
                    [
                        doc_id.lower(),
                        str(data.get("name") or "").lower(),
                        str(data.get("operator_name") or "").lower(),
                        str(data.get("driver") or "").lower(),
                    ]
                )
                if candidate_id and candidate_id in haystack:
                    return doc_id, label
            return None

        try:
            matched = await asyncio.to_thread(_resolve_ute_doc)
            if matched:
                resolved_id, display_label = matched
                items = await self.get_ute_inventory(resolved_id)
                return resolved_id, display_label, items
        except Exception as exc:
            logger.warning(
                "Error searching `utes` collection for '%s': %s", identifier, exc
            )

        return raw, raw, []

    async def search_materials(
        self,
        query: str = "",
        limit: int = 30,
        query_vector: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Search materials in Firebase (`materials` and `inventory` collections).

        Supports Firestore Vector Search (`find_nearest` on the `embedding` field) when
        `query_vector` is provided, and tokenized / substring ranking for text queries.
        """

        def _search_sync() -> list[dict[str, Any]]:
            catalog_by_id: dict[str, dict[str, Any]] = {
                item["id"]: dict(item) for item in DEFAULT_CATALOG_MATERIALS
            }

            # 1. If a query vector is provided, try Firestore native vector search first
            if query_vector:
                try:
                    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
                    from google.cloud.firestore_v1.vector import Vector

                    vec_query = self.db.collection("materials").find_nearest(
                        vector_field="embedding",
                        query_vector=Vector(query_vector),
                        distance_measure=DistanceMeasure.COSINE,
                        limit=limit,
                    )
                    vec_results: list[dict[str, Any]] = []
                    for doc in vec_query.stream():
                        raw = doc.to_dict() or {}
                        item_id = str(raw.get("item_id") or doc.id)
                        vec_results.append(
                            {
                                "id": item_id,
                                "item_id": item_id,
                                "sku": str(raw.get("sku") or item_id.upper()),
                                "name": str(raw.get("name") or item_id),
                                "unit": str(raw.get("unit") or "Units"),
                                "unit_price": float(raw.get("unit_price") or 0.0),
                            }
                        )
                    if vec_results:
                        return vec_results
                except Exception as vec_exc:
                    logger.warning(
                        "Vector search fallback to text search: %s", vec_exc
                    )

            # 2. Load materials from `materials` and `inventory` collections
            for col_name in ("materials", "catalog_items", "inventory"):
                try:
                    for doc in self.db.collection(col_name).stream():
                        raw = doc.to_dict() or {}
                        item_id = str(raw.get("item_id") or doc.id)
                        catalog_by_id[item_id] = {
                            "id": item_id,
                            "item_id": item_id,
                            "sku": str(raw.get("sku") or item_id.upper()),
                            "name": str(raw.get("name") or item_id),
                            "unit": str(raw.get("unit") or "Units"),
                            "unit_price": float(raw.get("unit_price") or 0.0),
                        }
                except Exception as col_exc:
                    logger.debug(
                        "Skipping collection '%s' during material search: %s",
                        col_name,
                        col_exc,
                    )

            all_items = list(catalog_by_id.values())
            q_clean = query.strip().lower()
            if not q_clean:
                return all_items[:limit]

            q_tokens = [tok for tok in re.split(r"\s+", q_clean) if tok]
            scored: list[tuple[int, dict[str, Any]]] = []

            for item in all_items:
                name_l = str(item.get("name", "")).lower()
                sku_l = str(item.get("sku", "")).lower()
                id_l = str(item.get("item_id", "")).lower()
                haystack = f"{name_l} {sku_l} {id_l}"

                score = 0
                if name_l == q_clean or sku_l == q_clean:
                    score += 100
                elif name_l.startswith(q_clean) or sku_l.startswith(q_clean):
                    score += 60
                elif q_clean in haystack:
                    score += 40

                token_hits = sum(1 for tok in q_tokens if tok in haystack)
                if token_hits == len(q_tokens):
                    score += 30
                elif token_hits > 0:
                    score += token_hits * 10

                if score > 0:
                    scored.append((score, item))

            scored.sort(key=lambda pair: (-pair[0], pair[1]["name"].lower()))
            return [item for _, item in scored[:limit]]

        try:
            return await asyncio.to_thread(_search_sync)
        except Exception as exc:
            logger.error("Error searching materials (query='%s'): %s", query, exc)
            raise FirestoreServiceError(
                f"Failed to search materials: {exc}"
            ) from exc

    async def create_material(
        self, payload: MaterialCreateRequest
    ) -> dict[str, Any]:
        """Create or update a material in the Firestore `materials` collection."""

        def _create_sync() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            clean_name = payload.name.strip()
            slug = re.sub(r"[^a-z0-9]+", "-", clean_name.lower()).strip("-")
            if not slug:
                slug = f"mat-{int(now.timestamp())}"

            sku = (payload.sku or slug).strip().upper()
            tokens = list(
                {
                    tok
                    for tok in re.split(r"[^a-z0-9]+", f"{clean_name.lower()} {sku.lower()}")
                    if tok
                }
            )

            doc_data: dict[str, Any] = {
                "id": slug,
                "item_id": slug,
                "sku": sku,
                "name": clean_name,
                "unit": payload.unit.strip() or "Units",
                "unit_price": float(payload.unit_price or 0.0),
                "search_tokens": tokens,
                "embedding": None,  # Populated asynchronously by vectorization pipeline
                "updated_at": now,
            }

            doc_ref = self.db.collection("materials").document(slug)
            doc_ref.set(doc_data, merge=True)
            return doc_data

        try:
            return await asyncio.to_thread(_create_sync)
        except Exception as exc:
            logger.error("Error creating material '%s': %s", payload.name, exc)
            raise FirestoreServiceError(
                f"Failed to create material '{payload.name}': {exc}"
            ) from exc

    async def deduct_ute_stock(
        self, ute_id: str, items: list[ShiftItemUsed]
    ) -> list[UteStockDeductionResult]:
        """Decrement stock in Firestore under `utes/{ute_id}/inventory/{item_id}`.

        Uses a Firestore transaction per deduction batch to avoid race conditions
        and records resulting stock levels and reorder alerts.
        """
        if not items:
            return []

        def _deduct_sync() -> list[UteStockDeductionResult]:
            now = datetime.now(timezone.utc)
            results: list[UteStockDeductionResult] = []
            inventory_col = (
                self.db.collection("utes").document(ute_id).collection("inventory")
            )

            transaction = self.db.transaction()

            @firestore.transactional
            def _run_in_transaction(
                txn: firestore.Transaction,
            ) -> list[UteStockDeductionResult]:
                tx_results: list[UteStockDeductionResult] = []
                snapshots: list[tuple[ShiftItemUsed, firestore.DocumentReference, Any]] = []

                # Read all item documents first (Firestore transactions require reads before writes)
                for item in items:
                    doc_ref = inventory_col.document(item.item_id)
                    snapshot = doc_ref.get(transaction=txn)
                    snapshots.append((item, doc_ref, snapshot))

                for item, doc_ref, snapshot in snapshots:
                    if snapshot.exists:
                        existing = snapshot.to_dict() or {}
                        prev_qty = float(existing.get("quantity", 0.0))
                        threshold = float(existing.get("reorder_threshold", 2.0))
                        new_qty = prev_qty - float(item.quantity)
                        txn.update(
                            doc_ref,
                            {
                                "quantity": firestore.Increment(-float(item.quantity)),
                                "sku": item.sku,
                                "name": item.name,
                                "updated_at": now,
                            },
                        )
                    else:
                        prev_qty = 0.0
                        threshold = 2.0
                        new_qty = -float(item.quantity)
                        txn.set(
                            doc_ref,
                            {
                                "ute_id": ute_id,
                                "item_id": item.item_id,
                                "sku": item.sku,
                                "name": item.name,
                                "quantity": new_qty,
                                "reorder_threshold": threshold,
                                "updated_at": now,
                            },
                        )

                    tx_results.append(
                        UteStockDeductionResult(
                            ute_id=ute_id,
                            item_id=item.item_id,
                            sku=item.sku,
                            name=item.name,
                            deducted_quantity=float(item.quantity),
                            previous_quantity=prev_qty,
                            remaining_quantity=new_qty,
                            reorder_threshold=threshold,
                            below_threshold=new_qty <= threshold,
                        )
                    )
                return tx_results

            results = _run_in_transaction(transaction)
            return results

        try:
            deduction_results = await asyncio.to_thread(_deduct_sync)
            for res in deduction_results:
                if res.below_threshold:
                    logger.warning(
                        "Low Ute stock alert on '%s' for item '%s' (%s): %.2f remaining (threshold %.2f)",
                        ute_id,
                        res.name,
                        res.sku,
                        res.remaining_quantity,
                        res.reorder_threshold,
                    )
            return deduction_results
        except Exception as exc:
            logger.error("Error deducting stock for Ute '%s': %s", ute_id, exc)
            raise FirestoreServiceError(
                f"Failed to deduct stock for Ute '{ute_id}': {exc}"
            ) from exc

    async def mark_shifts_as_invoiced(
        self, shift_ids: list[str], invoice_id: str
    ) -> int:
        """Mark the given shift document IDs as `invoiced=True` and attach `invoice_id`."""
        if not shift_ids:
            return 0

        def _batch_mark() -> int:
            now = datetime.now(timezone.utc)
            shifts_col = self.db.collection("shifts")
            updated_count = 0

            # Firestore WriteBatch supports up to 500 operations per batch
            chunk_size = 450
            for idx in range(0, len(shift_ids), chunk_size):
                chunk = shift_ids[idx : idx + chunk_size]
                batch = self.db.batch()
                for shift_id in chunk:
                    doc_ref = shifts_col.document(shift_id)
                    batch.set(
                        doc_ref,
                        {
                            "invoiced": True,
                            "invoice_id": invoice_id,
                            "invoiced_at": now,
                        },
                        merge=True,
                    )
                    updated_count += 1
                batch.commit()
            return updated_count

        try:
            count = await asyncio.to_thread(_batch_mark)
            logger.info(
                "Marked %d shift(s) as invoiced under QuickBooks invoice '%s'",
                count,
                invoice_id,
            )
            return count
        except Exception as exc:
            logger.error(
                "Error marking shifts %s as invoiced for invoice '%s': %s",
                shift_ids,
                invoice_id,
                exc,
            )
            raise FirestoreServiceError(
                f"Failed to mark shifts as invoiced: {exc}"
            ) from exc

    async def get_qbo_tokens(self) -> dict[str, Any] | None:
        """Retrieve persisted QuickBooks OAuth2 tokens from Firestore `integrations/quickbooks`."""

        def _read_tokens() -> dict[str, Any] | None:
            doc = self.db.collection("integrations").document("quickbooks").get()
            if doc.exists:
                return doc.to_dict()
            return None

        try:
            return await asyncio.to_thread(_read_tokens)
        except Exception as exc:
            logger.warning(
                "Could not read QuickBooks tokens from Firestore (falling back to settings): %s",
                exc,
            )
            return None

    async def save_qbo_tokens(
        self, access_token: str, refresh_token: str, expires_in: int = 3600
    ) -> None:
        """Persist refreshed QuickBooks OAuth2 tokens in Firestore `integrations/quickbooks`."""

        def _write_tokens() -> None:
            now = datetime.now(timezone.utc)
            self.db.collection("integrations").document("quickbooks").set(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": expires_in,
                    "updated_at": now,
                },
                merge=True,
            )

        try:
            await asyncio.to_thread(_write_tokens)
        except Exception as exc:
            logger.warning(
                "Could not persist QuickBooks tokens to Firestore: %s", exc
            )


firestore_service = FirestoreService()
