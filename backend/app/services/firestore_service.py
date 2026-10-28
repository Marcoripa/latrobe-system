import asyncio
import logging
import re
import time
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

    CACHE_TTL_SECONDS: float = 60.0

    def __init__(self) -> None:
        self._db: firestore.Client | None = None
        self._operator_cache: dict[str, tuple[str | None, str | None]] = {}
        self._projects_cache: list[dict[str, Any]] | None = None
        self._projects_cache_ts: float = 0.0
        self._materials_cache: list[dict[str, Any]] | None = None
        self._materials_cache_ts: float = 0.0
        self._suppliers_cache: list[dict[str, Any]] | None = None
        self._suppliers_cache_ts: float = 0.0

    def invalidate_projects_cache(self) -> None:
        """Clear the in-memory projects cache."""
        self._projects_cache = None
        self._projects_cache_ts = 0.0

    def invalidate_materials_cache(self) -> None:
        """Clear the in-memory materials cache."""
        self._materials_cache = None
        self._materials_cache_ts = 0.0

    def invalidate_suppliers_cache(self) -> None:
        """Clear the in-memory suppliers cache."""
        self._suppliers_cache = None
        self._suppliers_cache_ts = 0.0

    def _get_all_projects_sync(self) -> list[dict[str, Any]]:
        """Return all projects using a short-lived in-memory TTL cache."""
        now_mono = time.monotonic()
        if (
            self._projects_cache is not None
            and (now_mono - self._projects_cache_ts) < self.CACHE_TTL_SECONDS
        ):
            return self._projects_cache

        col_ref = self.db.collection("projects")
        projects: list[dict[str, Any]] = []
        for doc in col_ref.stream():
            data = doc.to_dict() or {}
            data["id"] = doc.id
            projects.append(data)
        self._projects_cache = projects
        self._projects_cache_ts = time.monotonic()
        return projects

    def _resolve_operator_info(self, uid: str | None) -> tuple[str | None, str | None]:
        """Resolve (display_name, email) from Firebase Auth for a given operator UID."""
        clean_uid = str(uid or "").strip()
        if not clean_uid or clean_uid == "anonymous-operator":
            return None, None
        if clean_uid in self._operator_cache:
            return self._operator_cache[clean_uid]
        try:
            from firebase_admin import auth as fb_auth

            initialize_firebase()
            user_rec = fb_auth.get_user(clean_uid)
            name = user_rec.display_name or (
                user_rec.email.split("@")[0] if user_rec.email else None
            )
            email = user_rec.email
            self._operator_cache[clean_uid] = (name, email)
            return name, email
        except Exception:
            self._operator_cache[clean_uid] = (None, None)
            return None, None

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
            self.invalidate_projects_cache()
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
            all_projs = self._get_all_projects_sync()
            if effective_status:
                eff_lower = effective_status.lower()
                filtered = [
                    dict(p)
                    for p in all_projs
                    if str(p.get("status") or "active").lower() == eff_lower
                ]
            else:
                filtered = [dict(p) for p in all_projs]
            if limit is not None:
                return filtered[:limit]
            return filtered

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
            # Check warm cache first to avoid unnecessary round-trips on non-ID strings
            now_mono = time.monotonic()
            if (
                self._projects_cache is not None
                and (now_mono - self._projects_cache_ts) < self.CACHE_TTL_SECONDS
            ):
                for p in self._projects_cache:
                    if p.get("id") == project_id:
                        return dict(p)
                return None

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
            payload.pop("ute_id", None)

            if not payload.get("operator_name") or not payload.get("operator_email"):
                resolved_name, resolved_email = self._resolve_operator_info(
                    payload.get("operator_id")
                )
                if not payload.get("operator_name") and resolved_name:
                    payload["operator_name"] = resolved_name
                if not payload.get("operator_email") and resolved_email:
                    payload["operator_email"] = resolved_email

            payload["logged_by"] = (
                payload.get("logged_by")
                or payload.get("operator_name")
                or payload.get("operator_email")
                or payload.get("operator_id")
            )

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
            self.invalidate_projects_cache()
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

    async def find_matching_projects(
        self, identifier: str
    ) -> list[dict[str, Any]]:
        """Find all matching projects by exact ID/name or partial name/client match.
        Returns a list of 1 item on an unambiguous match, multiple items on an ambiguous match,
        or an empty list if no project matches.
        """
        clean_id = identifier.strip()
        if not clean_id:
            return []

        def _search_projects() -> list[dict[str, Any]]:
            all_projects = self._get_all_projects_sync()

            # 1. Check direct document ID match first
            for p in all_projects:
                if str(p.get("id")) == clean_id:
                    return [dict(p)]

            needle = clean_id.lower()
            normalized_needle = (
                needle.replace("project", "").replace("job", "").strip() or needle
            )
            alnum_needle = re.sub(r"[^a-z0-9]", "", normalized_needle)
            exact_matches: list[dict[str, Any]] = []
            partial_matches: list[dict[str, Any]] = []

            for p in all_projects:
                data = dict(p)
                doc_id = str(data.get("id") or "")
                doc_name = str(data.get("name") or "").lower()
                client_name = str(data.get("client_name") or "").lower()
                doc_id_lower = doc_id.lower()
                alnum_doc_name = re.sub(r"[^a-z0-9]", "", doc_name)
                alnum_client_name = re.sub(r"[^a-z0-9]", "", client_name)

                # Exact project name or ID match
                if (
                    needle in (doc_name, doc_id_lower)
                    or (normalized_needle and normalized_needle == doc_name)
                    or (alnum_needle and alnum_needle == alnum_doc_name)
                ):
                    exact_matches.append(data)
                    continue

                # Client name or partial project name match
                if normalized_needle and (
                    needle == client_name
                    or normalized_needle in doc_name
                    or normalized_needle in client_name
                    or (doc_name and doc_name in normalized_needle)
                    or (
                        alnum_needle
                        and (
                            alnum_needle in alnum_doc_name
                            or alnum_needle in alnum_client_name
                            or (alnum_doc_name and alnum_doc_name in alnum_needle)
                        )
                    )
                ):
                    partial_matches.append(data)

            return exact_matches if exact_matches else partial_matches

        try:
            return await asyncio.to_thread(_search_projects)
        except Exception as exc:
            logger.error("Error searching project for '%s': %s", identifier, exc)
            raise FirestoreServiceError(
                f"Failed to search project '{identifier}': {exc}"
            ) from exc

    async def find_project_by_name_or_id(
        self, identifier: str
    ) -> dict[str, Any] | None:
        """Resolve a single project by exact Firestore document ID or case-insensitive name/client match."""
        matches = await self.find_matching_projects(identifier)
        return matches[0] if matches else None

    async def get_project_status(self, project_identifier: str) -> dict[str, Any]:
        """Fetch comprehensive project details, client info, location, logged hours by category,
        aggregated materials/items used, and field notes from Firestore.
        Detects ambiguity when multiple projects match and provides candidate suggestions when none match.
        """
        matches = await self.find_matching_projects(project_identifier)
        if not matches:
            active_projects = await self.get_active_projects()
            suggestions = [
                {
                    "project_id": str(p.get("id")),
                    "name": p.get("name"),
                    "client_name": p.get("client_name"),
                }
                for p in active_projects[:15]
            ]
            return {
                "found": False,
                "ambiguous": False,
                "error": f"Project '{project_identifier}' was not found.",
                "available_projects": suggestions,
                "instruction": (
                    "Inform the user that the requested project was not found. "
                    "If any project in `available_projects` looks similar, ask if they meant that one."
                ),
            }

        if len(matches) > 1:
            candidates = [
                {
                    "project_id": str(m.get("id")),
                    "name": m.get("name"),
                    "client_name": m.get("client_name"),
                    "status": m.get("status", "Active"),
                    "location": m.get("location"),
                }
                for m in matches
            ]
            return {
                "found": False,
                "ambiguous": True,
                "query": project_identifier,
                "candidates": candidates,
                "instruction": (
                    f"Multiple projects ({len(candidates)}) matched '{project_identifier}'. "
                    "Do NOT guess which one the user wants. List these candidate projects clearly "
                    "and ask the user to clarify which specific project they are referring to."
                ),
            }

        project = matches[0]

        project_id = str(project.get("id"))
        shifts = await self.get_project_shifts(project_id, only_unbilled=False)

        total_hours = 0.0
        unbilled_hours = 0.0
        billed_hours = 0.0
        unbilled_shifts_count = 0
        hours_by_category: dict[str, float] = {}
        hours_by_operator: dict[str, float] = {}
        items_map: dict[str, dict[str, Any]] = {}
        field_notes: list[dict[str, Any]] = []
        shift_logs: list[dict[str, Any]] = []

        for s in shifts:
            hrs = float(s.get("hours") or 0.0)
            cat = str(s.get("category") or "On-Site")
            is_invoiced = bool(s.get("invoiced"))
            op_id = str(s.get("operator_id") or "")
            op_name = s.get("operator_name") or s.get("logged_by")
            op_email = s.get("operator_email")
            if not op_name and op_id:
                op_name, resolved_email = await asyncio.to_thread(
                    self._resolve_operator_info, op_id
                )
                op_email = op_email or resolved_email
            logged_by = str(op_name or op_email or op_id or "Unknown Operator")

            created_at = s.get("created_at")
            ts_str = (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at or "")
            )

            total_hours += hrs
            hours_by_category[cat] = round(hours_by_category.get(cat, 0.0) + hrs, 2)
            hours_by_operator[logged_by] = round(
                hours_by_operator.get(logged_by, 0.0) + hrs, 2
            )

            if is_invoiced:
                billed_hours += hrs
            else:
                unbilled_hours += hrs
                unbilled_shifts_count += 1

            shift_items: list[dict[str, Any]] = []
            # Aggregate items/materials used across shifts
            for item in s.get("items_used") or []:
                if not isinstance(item, dict):
                    continue
                sku = str(
                    item.get("sku")
                    or item.get("item_id")
                    or item.get("name")
                    or "UNKNOWN"
                ).strip()
                qty = float(item.get("quantity") or 0.0)
                if qty <= 0:
                    continue
                shift_items.append(
                    {
                        "sku": sku,
                        "name": str(item.get("name") or sku),
                        "quantity": qty,
                    }
                )
                if sku not in items_map:
                    items_map[sku] = {
                        "item_id": item.get("item_id"),
                        "sku": sku,
                        "name": str(item.get("name") or sku),
                        "total_quantity": 0.0,
                        "unbilled_quantity": 0.0,
                    }
                items_map[sku]["total_quantity"] = round(
                    items_map[sku]["total_quantity"] + qty, 2
                )
                if not is_invoiced:
                    items_map[sku]["unbilled_quantity"] = round(
                        items_map[sku]["unbilled_quantity"] + qty, 2
                    )

            note_text = str(s.get("notes") or "").strip()
            clean_note = note_text if (note_text and note_text.lower() != "none") else None

            shift_logs.append(
                {
                    "shift_id": s.get("id"),
                    "date": ts_str,
                    "logged_by": logged_by,
                    "operator_name": op_name or logged_by,
                    "operator_email": op_email,
                    "operator_id": op_id,
                    "category": cat,
                    "hours": hrs,
                    "items_used": shift_items,
                    "notes": clean_note,
                    "invoiced": is_invoiced,
                }
            )

            # Collect operator field notes
            if clean_note:
                field_notes.append(
                    {
                        "date": ts_str,
                        "logged_by": logged_by,
                        "category": cat,
                        "hours": hrs,
                        "note": clean_note,
                        "invoiced": is_invoiced,
                    }
                )

        shift_logs.sort(key=lambda x: str(x.get("date") or ""))

        return {
            "found": True,
            "project_id": project_id,
            "qbo_id": project.get("qbo_id"),
            "name": str(project.get("name") or project_id),
            "client_name": str(project.get("client_name") or "N/A"),
            "status": str(project.get("status") or "active"),
            "location": project.get("location") or project.get("address"),
            "hourly_rate": project.get("hourly_rate"),
            "total_hours": round(total_hours, 2),
            "unbilled_hours": round(unbilled_hours, 2),
            "billed_hours": round(billed_hours, 2),
            "hours_by_category": hours_by_category,
            "hours_by_operator": hours_by_operator,
            "shifts_count": len(shifts),
            "unbilled_shifts_count": unbilled_shifts_count,
            "shifts": shift_logs[-30:],
            "items_used": list(items_map.values()),
            "items_used_count": len(items_map),
            "field_notes": field_notes[-20:],
        }

    async def get_shifts_activity(
        self,
        project_identifier: str | None = None,
        operator_query: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Query shift logs to track who did what and when, filtered by project and/or operator."""
        target_project_id: str | None = None
        project_map: dict[str, str] = {}

        if project_identifier and project_identifier.strip():
            matches = await self.find_matching_projects(project_identifier)
            if not matches:
                return {
                    "found": False,
                    "error": f"Project '{project_identifier}' was not found.",
                }
            if len(matches) > 1:
                return {
                    "found": False,
                    "ambiguous": True,
                    "candidates": [
                        {
                            "project_id": str(m.get("id")),
                            "name": m.get("name"),
                            "client_name": m.get("client_name"),
                        }
                        for m in matches
                    ],
                }
            target_project_id = str(matches[0].get("id"))
            project_map[target_project_id] = str(matches[0].get("name") or target_project_id)
        else:
            all_projs = await self.list_projects()
            for p in all_projs:
                project_map[str(p.get("id"))] = str(p.get("name") or p.get("id"))

        def _fetch_shifts() -> list[dict[str, Any]]:
            col = self.db.collection("shifts")
            query: Any = (
                col.where(filter=FieldFilter("project_id", "==", target_project_id))
                if target_project_id
                else col
            )
            raw_list: list[dict[str, Any]] = []
            for doc in query.stream():
                d = doc.to_dict() or {}
                d["id"] = doc.id
                raw_list.append(d)
            return raw_list

        raw_shifts = await asyncio.to_thread(_fetch_shifts)
        op_needle = (operator_query or "").strip().lower()
        results: list[dict[str, Any]] = []

        for s in raw_shifts:
            op_id = str(s.get("operator_id") or "")
            op_name = s.get("operator_name") or s.get("logged_by")
            op_email = s.get("operator_email")
            if not op_name and op_id:
                op_name, resolved_email = await asyncio.to_thread(
                    self._resolve_operator_info, op_id
                )
                op_email = op_email or resolved_email
            logged_by = str(op_name or op_email or op_id or "Unknown Operator")

            if op_needle:
                haystack = " ".join(
                    [
                        logged_by.lower(),
                        str(op_name or "").lower(),
                        str(op_email or "").lower(),
                        op_id.lower(),
                    ]
                )
                if op_needle not in haystack:
                    continue

            created_at = s.get("created_at")
            ts_str = (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at or "")
            )
            pid = str(s.get("project_id") or "")
            results.append(
                {
                    "shift_id": s.get("id"),
                    "date": ts_str,
                    "project_id": pid,
                    "project_name": project_map.get(pid, pid),
                    "logged_by": logged_by,
                    "operator_name": op_name or logged_by,
                    "operator_email": op_email,
                    "operator_id": op_id,
                    "category": str(s.get("category") or "On-Site"),
                    "hours": float(s.get("hours") or 0.0),
                    "items_used": [
                        {
                            "sku": item.get("sku"),
                            "name": item.get("name"),
                            "quantity": item.get("quantity"),
                        }
                        for item in (s.get("items_used") or [])
                        if isinstance(item, dict)
                    ],
                    "notes": s.get("notes"),
                    "invoiced": bool(s.get("invoiced")),
                }
            )

        results.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
        total_hours = round(sum(r["hours"] for r in results), 2)

        return {
            "found": True,
            "total_matching_shifts": len(results),
            "total_hours": total_hours,
            "shifts": results[:limit],
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

            # 2. Load materials from `materials` and `inventory` collections (with 60s TTL cache)
            now_mono = time.monotonic()
            if (
                self._materials_cache is not None
                and (now_mono - self._materials_cache_ts) < self.CACHE_TTL_SECONDS
            ):
                all_items = self._materials_cache
            else:
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
                self._materials_cache = all_items
                self._materials_cache_ts = time.monotonic()
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
            self.invalidate_materials_cache()
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

    @staticmethod
    def _normalize_phone(raw_phone: str) -> str:
        """Normalize phone strings for comparison (handles +61 vs 04 prefixes)."""
        digits = re.sub(r"\D", "", raw_phone or "")
        if digits.startswith("61") and len(digits) >= 11:
            return digits[2:]
        if digits.startswith("0") and len(digits) >= 10:
            return digits[1:]
        return digits

    async def find_customer_by_sender(
        self, sender_id: str
    ) -> dict[str, Any] | None:
        """Look up a customer profile in Firestore `customers` collection by phone, email, or ID."""
        cleaned = (sender_id or "").strip()
        if not cleaned:
            return None

        needle_lower = " ".join(cleaned.lower().split())
        needle_alnum = re.sub(r"[^a-z0-9]", "", needle_lower)
        needle_phone = self._normalize_phone(cleaned)

        def _lookup() -> dict[str, Any] | None:
            col = self.db.collection("customers")
            # 1. Direct document ID check
            direct_doc = col.document(cleaned).get()
            if direct_doc.exists:
                d = direct_doc.to_dict() or {}
                d["id"] = direct_doc.id
                return d

            # 2. Scan customers collection for matching email, phone, mobile, or name
            for doc in col.stream():
                data = doc.to_dict() or {}
                data["id"] = doc.id

                doc_email = str(data.get("email") or "").strip().lower()
                doc_name = " ".join(
                    str(data.get("display_name") or data.get("name") or "").lower().split()
                )
                doc_name_alnum = re.sub(r"[^a-z0-9]", "", doc_name)
                doc_qbo_id = str(data.get("qbo_id") or data.get("customer_id") or doc.id).strip().lower()

                if needle_lower and (
                    needle_lower in (doc_email, doc_name, doc_qbo_id)
                    or (needle_alnum and len(needle_alnum) >= 4 and needle_alnum == doc_name_alnum)
                ):
                    return data

                # Phone/mobile comparison
                if needle_phone and len(needle_phone) >= 7:
                    for field in ("phone", "mobile", "sender_id"):
                        val = str(data.get(field) or "")
                        norm_val = self._normalize_phone(val)
                        if norm_val and (
                            norm_val == needle_phone
                            or norm_val.endswith(needle_phone)
                            or needle_phone.endswith(norm_val)
                        ):
                            return data
            return None

        try:
            return await asyncio.to_thread(_lookup)
        except Exception as exc:
            logger.error("Error looking up customer for sender_id '%s': %s", sender_id, exc)
            return None

    async def get_customer_projects(
        self, customer_id: str
    ) -> list[dict[str, Any]]:
        """Retrieve all projects linked to a given QBO/Firestore customer_id."""
        clean_cid = str(customer_id or "").strip()
        if not clean_cid:
            return []

        def _fetch() -> list[dict[str, Any]]:
            col = self.db.collection("projects")
            results: dict[str, dict[str, Any]] = {}
            for field in ("client_id", "parent_customer_id", "qbo_customer_id"):
                for doc in col.where(filter=FieldFilter(field, "==", clean_cid)).stream():
                    d = doc.to_dict() or {}
                    d["id"] = doc.id
                    results[doc.id] = d
            return list(results.values())

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.warning("Error fetching projects for customer '%s': %s", customer_id, exc)
            return []

    async def save_booking_proposal(
        self, proposal_id: str, proposal_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Save a pending Human-in-the-Loop booking proposal in `booking_proposals/{proposal_id}`."""

        def _save() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            payload = dict(proposal_data)
            payload["proposal_id"] = proposal_id
            payload.setdefault("status", "pending_approval")
            payload["created_at"] = now
            payload["updated_at"] = now
            self.db.collection("booking_proposals").document(proposal_id).set(payload, merge=True)
            return payload

        try:
            return await asyncio.to_thread(_save)
        except Exception as exc:
            logger.error("Error saving booking proposal '%s': %s", proposal_id, exc)
            raise FirestoreServiceError(f"Failed to save booking proposal: {exc}") from exc

    async def get_booking_proposal(
        self, proposal_id: str
    ) -> dict[str, Any] | None:
        """Retrieve a booking proposal from `booking_proposals/{proposal_id}`."""

        def _get() -> dict[str, Any] | None:
            doc = self.db.collection("booking_proposals").document(proposal_id).get()
            if not doc.exists:
                return None
            data = doc.to_dict() or {}
            data["proposal_id"] = doc.id
            return data

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error("Error reading booking proposal '%s': %s", proposal_id, exc)
            raise FirestoreServiceError(f"Failed to read booking proposal: {exc}") from exc

    async def update_booking_proposal(
        self, proposal_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a booking proposal document in `booking_proposals/{proposal_id}`."""

        def _update() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            doc_ref = self.db.collection("booking_proposals").document(proposal_id)
            payload = dict(updates)
            payload["updated_at"] = now
            doc_ref.set(payload, merge=True)
            snap = doc_ref.get()
            data = snap.to_dict() or {}
            data["proposal_id"] = snap.id
            return data

        try:
            return await asyncio.to_thread(_update)
        except Exception as exc:
            logger.error("Error updating booking proposal '%s': %s", proposal_id, exc)
            raise FirestoreServiceError(f"Failed to update booking proposal: {exc}") from exc

    async def create_project_booking(
        self,
        project_id: str,
        booking_data: dict[str, Any],
        booking_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a confirmed booking document under `projects/{project_id}/bookings/{booking_id}`."""
        target_project_id = str(project_id or "unassigned").strip() or "unassigned"

        def _create() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            bookings_col = (
                self.db.collection("projects")
                .document(target_project_id)
                .collection("bookings")
            )
            doc_ref = (
                bookings_col.document(booking_id)
                if booking_id
                else bookings_col.document()
            )
            payload = dict(booking_data)
            payload.update(
                {
                    "booking_id": doc_ref.id,
                    "project_id": target_project_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            doc_ref.set(payload, merge=True)
            return payload

        try:
            return await asyncio.to_thread(_create)
        except Exception as exc:
            logger.error(
                "Error creating booking under projects/%s/bookings: %s",
                target_project_id,
                exc,
            )
            raise FirestoreServiceError(
                f"Failed to create booking for project '{target_project_id}': {exc}"
            ) from exc

    async def list_pending_booking_proposals(self) -> list[dict[str, Any]]:
        """Retrieve all booking proposals currently in `pending_approval` status
        so concurrent incoming requests do not propose overlapping time slots.
        """

        def _list_pending() -> list[dict[str, Any]]:
            col = self.db.collection("booking_proposals")
            query = col.where(filter=FieldFilter("status", "==", "pending_approval"))
            results: list[dict[str, Any]] = []
            for doc in query.stream():
                d = doc.to_dict() or {}
                d["proposal_id"] = doc.id
                results.append(
                    {
                        "proposal_id": doc.id,
                        "customer_name": d.get("customer_name"),
                        "project_name": d.get("project_name"),
                        "summary": d.get("summary"),
                        "start_iso": d.get("start_iso"),
                        "end_iso": d.get("end_iso"),
                        "proposed_slot": d.get("proposed_slot"),
                    }
                )
            return results

        try:
            return await asyncio.to_thread(_list_pending)
        except Exception as exc:
            logger.warning("Error listing pending booking proposals: %s", exc)
            return []

    async def is_email_processed(self, email_key: str) -> bool:
        """Check whether an email Message-ID / UID key has already been processed."""

        def _check() -> bool:
            doc = self.db.collection("processed_emails").document(email_key).get()
            return doc.exists

        try:
            return await asyncio.to_thread(_check)
        except Exception as exc:
            logger.warning("Error checking processed_emails for '%s': %s", email_key, exc)
            return False

    async def mark_email_processed(
        self, email_key: str, metadata: dict[str, Any]
    ) -> None:
        """Record an email Message-ID / UID key in `processed_emails/{email_key}`."""

        def _mark() -> None:
            payload = dict(metadata)
            payload["processed_at"] = datetime.now(timezone.utc)
            self.db.collection("processed_emails").document(email_key).set(
                payload, merge=True
            )

        try:
            await asyncio.to_thread(_mark)
        except Exception as exc:
            logger.warning("Error marking email '%s' as processed: %s", email_key, exc)

    async def list_suppliers(self) -> list[dict[str, Any]]:
        """Retrieve all active suppliers from Firestore `suppliers` collection (with 60s TTL cache)."""

        def _fetch_suppliers() -> list[dict[str, Any]]:
            now_mono = time.monotonic()
            if (
                self._suppliers_cache is not None
                and (now_mono - self._suppliers_cache_ts) < self.CACHE_TTL_SECONDS
            ):
                return self._suppliers_cache

            col = self.db.collection("suppliers")
            suppliers: list[dict[str, Any]] = []
            for doc in col.stream():
                data = doc.to_dict() or {}
                data["id"] = doc.id
                data.setdefault("vendor_id", doc.id)
                suppliers.append(data)
            self._suppliers_cache = suppliers
            self._suppliers_cache_ts = time.monotonic()
            return suppliers

        try:
            return await asyncio.to_thread(_fetch_suppliers)
        except Exception as exc:
            logger.error("Error listing suppliers from Firestore: %s", exc)
            return []

    async def save_pending_bill(
        self, bill_id: str, bill_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Save an extracted supplier invoice pending approval into `pending_bills/{bill_id}`."""

        def _save() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            payload = dict(bill_data)
            payload["bill_id"] = bill_id
            payload.setdefault("status", "pending_approval")
            payload["created_at"] = now
            payload["updated_at"] = now
            self.db.collection("pending_bills").document(bill_id).set(payload, merge=True)
            return payload

        try:
            return await asyncio.to_thread(_save)
        except Exception as exc:
            logger.error("Error saving pending bill '%s': %s", bill_id, exc)
            raise FirestoreServiceError(f"Failed to save pending bill: {exc}") from exc

    async def get_pending_bill(self, bill_id: str) -> dict[str, Any] | None:
        """Retrieve a pending bill document from `pending_bills/{bill_id}`."""

        def _get() -> dict[str, Any] | None:
            doc = self.db.collection("pending_bills").document(bill_id).get()
            if not doc.exists:
                return None
            data = doc.to_dict() or {}
            data["bill_id"] = doc.id
            return data

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error("Error reading pending bill '%s': %s", bill_id, exc)
            raise FirestoreServiceError(f"Failed to read pending bill: {exc}") from exc

    async def delete_pending_bill(self, bill_id: str) -> None:
        """Delete a document from `pending_bills/{bill_id}` after approval or rejection."""

        def _delete() -> None:
            self.db.collection("pending_bills").document(bill_id).delete()

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            logger.error("Error deleting pending bill '%s': %s", bill_id, exc)
            raise FirestoreServiceError(f"Failed to delete pending bill: {exc}") from exc


firestore_service = FirestoreService()
