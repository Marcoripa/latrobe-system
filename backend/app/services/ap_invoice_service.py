"""AI-Powered Accounts Payable (Supplier Invoice) Extraction & Approval Service.

Implements the 4-stage pipeline:
1. QBO Supplier Whitelist (`suppliers` collection in Firestore)
2. 3-Tier Gmail Filter (Direct Email Match -> Domain Match -> Accounting SaaS + Gemini Triage)
3. PDF Extraction Pipeline via Gemini File API (`gemini-3.8-flash`)
4. Human-in-the-Loop Telegram Approval Card (`approve_bill|<doc_id>`, `reject_bill|<doc_id>`) & QBO Bill Sync
"""

import html
import json
import logging
import re
import uuid
from typing import Any

import httpx

from app.core.config import settings
from app.services.firestore_service import FirestoreService, firestore_service
from app.services.quickbooks_service import QuickBooksService, quickbooks_service
from app.services.telegram_service import TelegramService, telegram_service

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"

# Generic ISP / consumer webmail domains that must NOT trigger Tier-2 company domain matching
# (Suppliers using these addresses are still matched on Tier-1 exact email address)
GENERIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.com.au",
        "hotmail.com",
        "hotmail.com.au",
        "live.com",
        "live.com.au",
        "msn.com",
        "yahoo.com",
        "yahoo.com.au",
        "icloud.com",
        "me.com",
        "bigpond.com",
        "bigpond.net.au",
        "telstra.com",
        "optusnet.com.au",
        "iinet.net.au",
        "tpg.com.au",
        "internode.on.net",
        "dodo.com.au",
        "wideband.net.au",
        "aussiebb.com.au",
        "protonmail.com",
        "proton.me",
    }
)

# Tier-3 known SaaS accounting / job-management platforms
ACCOUNTING_SAAS_DOMAINS: tuple[str, ...] = (
    "post.xero.com",
    "xero.com",
    "apps.myob.com",
    "myob.com",
    "myob.com.au",
    "quickbooks.com",
    "intuit.com",
    "notification.intuit.com",
    "reckon.com",
    "reckonhosted.com",
    "servicem8.com",
    "simpro.co",
    "simprogroup.com",
    "aroflo.com",
    "tradifyhq.com",
    "fergus.com",
    "freshbooks.com",
    "zoho.com",
    "zohobooks.com",
    "sage.com",
    "net-suite.com",
    "netsuite.com",
)

PDF_EXTRACTION_PROMPT = (
    "You are an expert Australian Accounts Payable AI assistant for LaTrobe Irrigation.\n"
    "Analyze the attached PDF supplier invoice and extract the invoice details.\n"
    "Return ONLY a valid JSON object matching this exact schema (no markdown fences, no commentary):\n"
    "{\n"
    '  "supplier_name": "String",\n'
    '  "invoice_number": "String",\n'
    '  "issue_date": "YYYY-MM-DD",\n'
    '  "due_date": "YYYY-MM-DD",\n'
    '  "total_amount_inc_gst": 0.0,\n'
    '  "gst_amount": 0.0,\n'
    '  "purchase_order_ref": "String (or null)"\n'
    "}\n\n"
    "Rules:\n"
    "- Convert all dates to ISO YYYY-MM-DD format (Australian invoices typically use DD/MM/YYYY).\n"
    "- If due_date is not explicitly stated on the invoice, set it equal to issue_date.\n"
    "- total_amount_inc_gst and gst_amount must be numeric floats (e.g. 125.50). If GST is not shown but invoice is a standard Australian Tax Invoice, GST is 1/11th of total_amount_inc_gst.\n"
    "- If no Purchase Order / PO / Job Reference is present, set purchase_order_ref to null."
)


def is_accounting_saas_domain(domain: str) -> bool:
    """Return True if `domain` belongs to a known SaaS accounting platform (Tier 3)."""
    dom = (domain or "").strip().lower()
    if not dom:
        return False
    for saas_dom in ACCOUNTING_SAAS_DOMAINS:
        if dom == saas_dom or dom.endswith("." + saas_dom):
            return True
    return False


def _normalize_alnum(val: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (val or "").lower())


class APInvoiceService:
    """Orchestrates 3-tier supplier email filtering, Gemini File API PDF extraction,
    Telegram Human-in-the-Loop approval cards, and QBO Bill creation.
    """

    def __init__(
        self,
        fs: FirestoreService | None = None,
        qbo: QuickBooksService | None = None,
        tg: TelegramService | None = None,
        timeout: float = 35.0,
    ) -> None:
        self._fs = fs or firestore_service
        self._qbo = qbo or quickbooks_service
        self._tg = tg or telegram_service
        self._timeout = timeout
        self._processing_bills: set[str] = set()

    @property
    def _model_name(self) -> str:
        resolved = settings.LLM_MODEL or "gemini-3.8-flash"
        if "gemini-2.5" in resolved:
            return "gemini-3.8-flash"
        return resolved

    def _resolve_supplier_by_name(
        self, candidate_name: str | None, suppliers: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Match an extracted supplier name against the Firestore `suppliers` whitelist."""
        if not candidate_name or not suppliers:
            return None

        clean = candidate_name.strip().lower()
        # Strip common Australian legal suffixes for matching
        stripped = re.sub(
            r"\b(pty\.?|ltd\.?|proprietary|limited|australia|aust|vic|group|holdings|trading)\b",
            "",
            clean,
        ).strip()
        alnum_full = _normalize_alnum(clean)
        alnum_stripped = _normalize_alnum(stripped) or alnum_full

        best_partial: dict[str, Any] | None = None
        for sup in suppliers:
            disp = str(sup.get("display_name") or "").strip()
            comp = str(sup.get("company_name") or "").strip()
            for target in (disp, comp):
                if not target:
                    continue
                t_low = target.lower()
                t_alnum = _normalize_alnum(t_low)
                if t_low == clean or (t_alnum and t_alnum == alnum_full):
                    return sup
                t_stripped = re.sub(
                    r"\b(pty\.?|ltd\.?|proprietary|limited|australia|aust|vic|group|holdings|trading)\b",
                    "",
                    t_low,
                ).strip()
                t_stripped_alnum = _normalize_alnum(t_stripped)
                if (
                    t_stripped_alnum
                    and alnum_stripped
                    and len(t_stripped_alnum) >= 4
                    and len(alnum_stripped) >= 4
                ):
                    if t_stripped_alnum == alnum_stripped:
                        return sup
                    if (
                        t_stripped_alnum in alnum_stripped
                        or alnum_stripped in t_stripped_alnum
                    ):
                        best_partial = best_partial or sup

        return best_partial

    async def _gemini_tier3_supplier_triage(
        self,
        *,
        from_name: str,
        subject: str,
        body_text: str,
        suppliers: list[dict[str, Any]],
    ) -> tuple[bool, dict[str, Any] | None]:
        """Tier 3: Ask Gemini whether an accounting-platform email body mentions any Firestore supplier."""
        supplier_names = sorted(
            {
                str(s.get("display_name") or "").strip()
                for s in suppliers
                if s.get("display_name")
            }
        )
        if not supplier_names:
            return False, None

        combined_context = f"From: {from_name}\nSubject: {subject}\n\n{body_text[:3000]}".strip()

        # Fast deterministic pre-check in Python first (saves an LLM call if the supplier name is verbatim in From/Subject/Body)
        direct_hit = self._resolve_supplier_by_name(from_name, suppliers)
        if direct_hit:
            return True, direct_hit

        if not settings.GEMINI_API_KEY:
            return False, None

        prompt = (
            f"Does this email body mention any of these suppliers: {json.dumps(supplier_names)}?\n"
            'Return JSON { "is_supplier": true/false, "supplier_name": "matched name or null" }.\n\n'
            f"EMAIL CONTENT:\n{combined_context}"
        )

        url = f"{GEMINI_API_BASE}/models/{self._model_name}:generateContent"
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "LOW"},
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url, params={"key": settings.GEMINI_API_KEY}, json=payload
                )
            if resp.status_code != 200:
                logger.warning("Gemini Tier-3 triage returned HTTP %d: %s", resp.status_code, resp.text)
                return False, None

            parts = (
                resp.json()
                .get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            raw_text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
            parsed = json.loads(cleaned)
            is_sup = bool(parsed.get("is_supplier") is True)
            matched_name = parsed.get("supplier_name")
            matched_sup = (
                self._resolve_supplier_by_name(str(matched_name), suppliers)
                if matched_name
                else None
            )
            return is_sup, matched_sup
        except Exception as exc:
            logger.warning("Error during Gemini Tier-3 supplier triage: %s", exc)
            return False, None

    async def evaluate_3tier_supplier_filter(
        self,
        *,
        from_email: str,
        from_name: str,
        subject: str,
        body_text: str,
    ) -> dict[str, Any]:
        """Evaluate the 3-Tier Gmail Supplier Filter:
        - Tier 1 (Direct Match): Sender email matches a Firestore supplier `email_address`.
        - Tier 2 (Domain Match): Sender domain matches a Firestore supplier `email_domain`.
        - Tier 3 (Accounting Catch-All): Sender domain is a known SaaS accounting platform
          and Gemini triage confirms the email mentions a known Firestore supplier.
        """
        addr = (from_email or "").strip().lower()
        domain = addr.split("@", 1)[1].strip().lower() if "@" in addr else ""

        suppliers = await self._fs.list_suppliers()

        # Tier 1: Direct Email Match
        if addr:
            for sup in suppliers:
                sup_email = str(sup.get("email_address") or "").strip().lower()
                sup_emails = [
                    str(e).strip().lower() for e in (sup.get("email_addresses") or [])
                ]
                if addr == sup_email or addr in sup_emails:
                    logger.info(
                        "Supplier Filter Tier 1 (Direct Match) passed for '%s' -> Vendor '%s' (%s)",
                        addr,
                        sup.get("display_name"),
                        sup.get("vendor_id"),
                    )
                    return {
                        "passed": True,
                        "tier": 1,
                        "tier_label": "tier1_direct_email",
                        "supplier": sup,
                    }

        # Tier 2: Domain Match (excluding generic ISP / consumer webmail domains)
        if domain and domain not in GENERIC_EMAIL_DOMAINS:
            for sup in suppliers:
                sup_domain = str(sup.get("email_domain") or "").strip().lower()
                if not sup_domain and sup.get("email_address") and "@" in str(sup.get("email_address")):
                    sup_domain = str(sup["email_address"]).split("@", 1)[1].strip().lower()
                if sup_domain and (
                    domain == sup_domain
                    or domain.endswith("." + sup_domain)
                    or sup_domain.endswith("." + domain)
                ):
                    logger.info(
                        "Supplier Filter Tier 2 (Domain Match) passed for '%s' (domain=%s) -> Vendor '%s' (%s)",
                        addr,
                        domain,
                        sup.get("display_name"),
                        sup.get("vendor_id"),
                    )
                    return {
                        "passed": True,
                        "tier": 2,
                        "tier_label": "tier2_domain_match",
                        "supplier": sup,
                    }

        # Tier 3: Accounting Catch-All (@post.xero.com, @apps.myob.com, @quickbooks.com, etc.)
        if is_accounting_saas_domain(domain):
            is_sup, matched_sup = await self._gemini_tier3_supplier_triage(
                from_name=from_name,
                subject=subject,
                body_text=body_text,
                suppliers=suppliers,
            )
            if is_sup:
                logger.info(
                    "Supplier Filter Tier 3 (Accounting SaaS Triage) passed for '%s' -> Vendor '%s'",
                    addr,
                    (matched_sup or {}).get("display_name"),
                )
                return {
                    "passed": True,
                    "tier": 3,
                    "tier_label": "tier3_accounting_saas",
                    "supplier": matched_sup,
                }

        return {
            "passed": False,
            "tier": None,
            "tier_label": "rejected_not_whitelisted_supplier",
            "supplier": None,
        }

    async def _upload_pdf_to_gemini_file_api(
        self, pdf_bytes: bytes, filename: str = "invoice.pdf"
    ) -> dict[str, str]:
        """Upload raw PDF bytes to the Gemini File API and return `{"uri": ..., "name": ...}`."""
        headers = {
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Header-Content-Length": str(len(pdf_bytes)),
            "X-Goog-Upload-Header-Content-Type": "application/pdf",
            "Content-Type": "application/pdf",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                GEMINI_UPLOAD_URL,
                params={"key": settings.GEMINI_API_KEY},
                headers=headers,
                content=pdf_bytes,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini File API upload failed (HTTP {resp.status_code}): {resp.text}"
            )
        file_info = resp.json().get("file", resp.json())
        file_uri = str(file_info.get("uri") or "")
        file_name = str(file_info.get("name") or "")
        if not file_uri:
            raise RuntimeError(f"Gemini File API did not return a file URI: {resp.text}")
        logger.info("Uploaded PDF '%s' (%d bytes) to Gemini File API: %s", filename, len(pdf_bytes), file_uri)
        return {"uri": file_uri, "name": file_name}

    async def _delete_gemini_file(self, file_name: str) -> None:
        """Delete temporary uploaded PDF from Gemini File API after extraction."""
        if not file_name:
            return
        url = f"{GEMINI_API_BASE}/{file_name}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(url, params={"key": settings.GEMINI_API_KEY})
        except Exception as exc:
            logger.debug("Non-critical error deleting temporary Gemini file '%s': %s", file_name, exc)

    async def extract_invoice_from_pdf(
        self, pdf_bytes: bytes, filename: str = "invoice.pdf"
    ) -> dict[str, Any]:
        """Upload PDF to Gemini File API and extract structured invoice JSON via Gemini-3.8-flash."""
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        uploaded = await self._upload_pdf_to_gemini_file_api(pdf_bytes, filename=filename)
        file_uri = uploaded["uri"]
        file_name = uploaded["name"]

        try:
            url = f"{GEMINI_API_BASE}/models/{self._model_name}:generateContent"
            payload: dict[str, Any] = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "file_data": {
                                    "mime_type": "application/pdf",
                                    "file_uri": file_uri,
                                }
                            },
                            {"text": PDF_EXTRACTION_PROMPT},
                        ],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingLevel": "LOW"},
                },
            }

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, params={"key": settings.GEMINI_API_KEY}, json=payload
                )

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Gemini PDF extraction returned HTTP {resp.status_code}: {resp.text}"
                )

            resp_data = resp.json()
            parts = (
                resp_data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            raw_text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
            extracted = json.loads(cleaned)
            if isinstance(extracted, list) and extracted:
                extracted = extracted[0]

            total_inc = round(float(extracted.get("total_amount_inc_gst") or 0.0), 2)
            gst_amt = round(float(extracted.get("gst_amount") or 0.0), 2)
            po_ref = extracted.get("purchase_order_ref")
            if isinstance(po_ref, str) and po_ref.strip().lower() in ("", "null", "none", "n/a"):
                po_ref = None

            return {
                "supplier_name": str(extracted.get("supplier_name") or "").strip(),
                "invoice_number": str(extracted.get("invoice_number") or "").strip(),
                "issue_date": str(extracted.get("issue_date") or "").strip(),
                "due_date": str(extracted.get("due_date") or extracted.get("issue_date") or "").strip(),
                "total_amount_inc_gst": total_inc,
                "gst_amount": gst_amt,
                "purchase_order_ref": str(po_ref).strip() if po_ref else None,
            }
        finally:
            await self._delete_gemini_file(file_name)

    @staticmethod
    def build_bill_approval_keyboard(bill_id: str) -> dict[str, Any]:
        """Construct the Stage 4 Inline Keyboard for supplier bill approval."""
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Approve & Sync to QBO",
                        "callback_data": f"approve_bill|{bill_id}",
                    }
                ],
                [
                    {
                        "text": "❌ Reject / Spam",
                        "callback_data": f"reject_bill|{bill_id}",
                    }
                ],
            ]
        }

    @staticmethod
    def format_bill_card_text(bill_data: dict[str, Any]) -> str:
        """Format the interactive Telegram card text for a draft supplier bill."""
        supplier_name = html.escape(
            str(bill_data.get("vendor_display_name") or bill_data.get("supplier_name") or "Unknown Supplier")
        )
        total_inc = float(bill_data.get("total_amount_inc_gst") or 0.0)
        gst_amt = float(bill_data.get("gst_amount") or 0.0)
        inv_num = html.escape(str(bill_data.get("invoice_number") or "N/A"))
        po_ref = bill_data.get("purchase_order_ref")
        po_str = html.escape(str(po_ref)) if po_ref else "None"

        return (
            f"🧾 <b>Draft Bill:</b> {supplier_name}\n"
            f"💰 <b>Amount:</b> ${total_inc:,.2f} (GST: ${gst_amt:,.2f})\n"
            f"📄 <b>Inv #:</b> {inv_num}\n"
            f"📌 <b>PO Ref:</b> {po_str}"
        )

    async def process_supplier_invoice_email(
        self,
        *,
        from_email: str,
        from_name: str,
        subject: str,
        pdf_attachments: list[dict[str, Any]],
        matched_supplier: dict[str, Any] | None,
        filter_tier: int | None,
    ) -> list[dict[str, Any]]:
        """Process all PDF attachments on a whitelisted supplier email:
        1. Extract structured invoice data via Gemini File API.
        2. Resolve QBO `vendor_id` from Firestore `suppliers`.
        3. Save pending bill in `pending_bills/{bill_id}`.
        4. Send interactive Telegram approval card to admin.
        """
        suppliers = await self._fs.list_suppliers()
        results: list[dict[str, Any]] = []

        for att in pdf_attachments:
            filename = str(att.get("filename") or "invoice.pdf")
            pdf_bytes: bytes = att.get("content") or b""
            if not pdf_bytes:
                continue

            extracted = await self.extract_invoice_from_pdf(pdf_bytes, filename=filename)

            # Resolve exact QBO Vendor from extracted supplier_name or Tier 1/2 matched supplier
            resolved_sup = (
                self._resolve_supplier_by_name(extracted.get("supplier_name"), suppliers)
                or matched_supplier
                or self._resolve_supplier_by_name(from_name, suppliers)
            )

            vendor_id = str((resolved_sup or {}).get("vendor_id") or "")
            vendor_display_name = str(
                (resolved_sup or {}).get("display_name")
                or extracted.get("supplier_name")
                or from_name
                or "Unknown Supplier"
            )

            bill_id = uuid.uuid4().hex[:12]
            bill_doc: dict[str, Any] = {
                "bill_id": bill_id,
                "vendor_id": vendor_id,
                "vendor_display_name": vendor_display_name,
                "supplier_name": extracted.get("supplier_name") or vendor_display_name,
                "invoice_number": extracted.get("invoice_number"),
                "issue_date": extracted.get("issue_date"),
                "due_date": extracted.get("due_date"),
                "total_amount_inc_gst": extracted.get("total_amount_inc_gst", 0.0),
                "gst_amount": extracted.get("gst_amount", 0.0),
                "purchase_order_ref": extracted.get("purchase_order_ref"),
                "from_email": from_email,
                "from_name": from_name,
                "subject": subject,
                "filename": filename,
                "filter_tier": filter_tier,
                "status": "pending_approval",
            }

            await self._fs.save_pending_bill(bill_id, bill_doc)

            card_text = self.format_bill_card_text(bill_doc)
            keyboard = self.build_bill_approval_keyboard(bill_id)

            tg_res = await self._tg.send_message(
                text=card_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            results.append(
                {
                    "bill_id": bill_id,
                    "vendor_id": vendor_id,
                    "supplier_name": vendor_display_name,
                    "invoice_number": bill_doc["invoice_number"],
                    "total_amount_inc_gst": bill_doc["total_amount_inc_gst"],
                    "telegram_sent": bool(tg_res.get("ok")),
                }
            )

        return results

    async def handle_bill_callback_query(
        self,
        callback_query_id: str,
        callback_data: str,
        chat_id: int | None,
        message_id: int | None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Handle Telegram inline button callbacks (`approve_bill|<doc_id>` / `reject_bill|<doc_id>`)."""
        if "|" not in callback_data:
            await self._tg.answer_callback_query(callback_query_id, text="Invalid bill action.")
            return {"ok": False, "reason": "invalid_callback_data"}

        action, bill_id = callback_data.split("|", 1)
        action = action.strip().lower()
        bill_id = bill_id.strip()

        # 1. Immediately deactivate inline keyboard buttons on first tap to prevent duplicate confirmations
        if chat_id is not None and message_id is not None:
            await self._tg.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )

        if bill_id in self._processing_bills:
            await self._tg.answer_callback_query(
                callback_query_id,
                text="⏳ This bill is already being processed...",
            )
            return {"ok": True, "status": "already_processing", "bill_id": bill_id}

        self._processing_bills.add(bill_id)
        try:
            bill_doc = await self._fs.get_pending_bill(bill_id)
            if not bill_doc:
                await self._tg.answer_callback_query(
                    callback_query_id,
                    text="This bill has already been processed.",
                    show_alert=True,
                )
                return {"ok": False, "reason": "pending_bill_not_found", "bill_id": bill_id}

            base_card = self.format_bill_card_text(bill_doc)

            if action == "reject_bill":
                await self._fs.delete_pending_bill(bill_id)
                await self._tg.answer_callback_query(
                    callback_query_id, text="❌ Bill rejected and removed."
                )
                updated_text = f"{base_card}\n\n❌ <b>Rejected / Marked as Spam</b>"
                if chat_id is not None and message_id is not None:
                    await self._tg.edit_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=updated_text,
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": []},
                    )
                return {"ok": True, "status": "rejected", "bill_id": bill_id}

            if action == "approve_bill":
                vendor_id = str(bill_doc.get("vendor_id") or "").strip()
                if not vendor_id:
                    suppliers = await self._fs.list_suppliers()
                    matched = self._resolve_supplier_by_name(
                        bill_doc.get("supplier_name") or bill_doc.get("vendor_display_name"),
                        suppliers,
                    )
                    if matched:
                        vendor_id = str(matched.get("vendor_id") or "").strip()

                if not vendor_id:
                    await self._tg.answer_callback_query(
                        callback_query_id,
                        text="⚠️ Could not find matching Vendor ID in QuickBooks.",
                        show_alert=True,
                    )
                    return {"ok": False, "reason": "missing_vendor_id", "bill_id": bill_id}

                await self._tg.answer_callback_query(
                    callback_query_id, text="⏳ Syncing Bill to QuickBooks..."
                )

                qbo_bill = await self._qbo.create_bill(
                    vendor_id=vendor_id,
                    total_amount_inc_gst=float(bill_doc.get("total_amount_inc_gst") or 0.0),
                    gst_amount=float(bill_doc.get("gst_amount") or 0.0),
                    invoice_number=bill_doc.get("invoice_number"),
                    issue_date=bill_doc.get("issue_date"),
                    due_date=bill_doc.get("due_date"),
                    purchase_order_ref=bill_doc.get("purchase_order_ref"),
                    description=f"Supplier Invoice - {bill_doc.get('vendor_display_name') or bill_doc.get('supplier_name')}",
                )
                qbo_bill_id = str(qbo_bill.get("Id") or "")
                bill_url = self._qbo.get_bill_review_url(qbo_bill_id) if qbo_bill_id else ""

                await self._fs.delete_pending_bill(bill_id)

                synced_footer = "✅ <b>Bill Synced to QBO</b>"
                if qbo_bill_id:
                    synced_footer += f" (ID: <code>{html.escape(qbo_bill_id)}</code>)"
                if bill_url:
                    synced_footer += f'\n🔗 <a href="{bill_url}">Open Bill in QuickBooks Online</a>'

                updated_text = f"{base_card}\n\n{synced_footer}"
                if chat_id is not None and message_id is not None:
                    await self._tg.edit_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=updated_text,
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": []},
                    )
                return {
                    "ok": True,
                    "status": "approved_and_synced",
                    "bill_id": bill_id,
                    "qbo_bill_id": qbo_bill_id,
                }

            await self._tg.answer_callback_query(callback_query_id, text="Unknown action.")
            return {"ok": False, "reason": "unknown_action"}
        except Exception as exc:
            logger.exception("Error handling bill callback query '%s': %s", callback_data, exc)
            await self._tg.answer_callback_query(
                callback_query_id,
                text=f"⚠️ Failed to sync bill: {str(exc)[:140]}",
                show_alert=True,
            )
            return {"ok": False, "error": str(exc), "bill_id": bill_id}
        finally:
            self._processing_bills.discard(bill_id)


ap_invoice_service = APInvoiceService()
