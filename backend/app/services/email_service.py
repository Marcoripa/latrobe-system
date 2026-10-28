"""Gmail IMAP (Inbound Polling) and SMTP (Outbound Confirmation) Service."""

import asyncio
import email
import hashlib
import imaplib
import logging
import re
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from app.core.config import settings
from app.schemas.booking import InboundMessage
from app.services.firestore_service import FirestoreService, firestore_service

logger = logging.getLogger(__name__)

# Pre-flight blocklists to prevent reading or analyzing banking, government, automated, or personal emails
BLOCKED_SENDER_PREFIXES: tuple[str, ...] = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "alerts@",
    "security@",
    "statements@",
    "estatements@",
    "notifications@",
    "mailer-daemon@",
    "postmaster@",
    "newsletter@",
    "marketing@",
    "promotions@",
    "updates@",
)

BLOCKED_SENDER_DOMAINS: tuple[str, ...] = (
    # Australian & major banks / financial institutions
    "commbank.com.au",
    "cba.com.au",
    "anz.com",
    "anz.com.au",
    "nab.com.au",
    "westpac.com.au",
    "macquarie.com",
    "ing.com.au",
    "bendigobank.com.au",
    "suncorp.com.au",
    "bankofmelbourne.com.au",
    "stgeorge.com.au",
    "ubank.com.au",
    "up.com.au",
    "hsbc.com.au",
    "hsbc.com",
    "citibank.com.au",
    "amex.com",
    "americanexpress.com",
    "paypal.com",
    "paypal.com.au",
    "stripe.com",
    "wise.com",
    "revolut.com",
    "afterpay.com",
    "zip.co",
    # Government & personal portals
    "my.gov.au",
    "ato.gov.au",
    "vicroads.vic.gov.au",
    "service.vic.gov.au",
    "servicesaustralia.gov.au",
    # Social, entertainment, retail & account security
    "accounts.google.com",
    "google.com",
    "facebookmail.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "netflix.com",
    "spotify.com",
    "apple.com",
    "amazon.com",
    "amazon.com.au",
    "ebay.com",
    "ebay.com.au",
)

BLOCKED_SUBJECT_KEYWORDS: tuple[str, ...] = (
    "bank statement",
    "account statement",
    "statement is ready",
    "estatement",
    "verification code",
    "security alert",
    "security code",
    "one-time passcode",
    "one time passcode",
    "otp",
    "password reset",
    "two-factor",
    "2fa",
    "new sign-in",
    "login attempt",
    "direct debit",
    "tax return",
    "mygov",
    "bpay",
    "payid",
    "netbank",
    "credit card",
    "debit card",
    "loan repayment",
    "unsubscribe",
)


def is_blocked_personal_or_banking_email(
    from_email: str, from_name: str, subject: str
) -> tuple[bool, str | None]:
    """Inspect only sender and subject headers to filter out banking, automated, or personal emails
    BEFORE downloading the email body or sending anything to Gemini.
    """
    addr = (from_email or "").strip().lower()
    subj = (subject or "").strip().lower()

    for prefix in BLOCKED_SENDER_PREFIXES:
        if addr.startswith(prefix):
            return True, f"blocked_sender_prefix:{prefix}"

    if "@" in addr:
        domain = addr.split("@", 1)[1]
        for blocked_dom in BLOCKED_SENDER_DOMAINS:
            if domain == blocked_dom or domain.endswith("." + blocked_dom):
                return True, f"blocked_sender_domain:{blocked_dom}"

    for kw in BLOCKED_SUBJECT_KEYWORDS:
        if kw in subj:
            return True, f"blocked_subject_keyword:{kw}"

    return False, None


def _decode_mime_words(raw_val: str | None) -> str:
    """Decode RFC 2047 encoded email headers (e.g. Subject, From) into UTF-8 text."""
    if not raw_val:
        return ""
    decoded_parts: list[str] = []
    for part, enc in decode_header(raw_val):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    return "".join(decoded_parts).strip()


def _extract_plain_body(msg: email.message.Message) -> str:
    """Extract clean plain-text body from a MIME email message, stripping HTML if needed."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disp = str(part.get("Content-Disposition") or "")
            if "attachment" in content_disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts).strip()

    if html_parts:
        raw_html = "\n".join(html_parts)
        stripped = re.sub(r"<style.*?>.*?</style>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r"<script.*?>.*?</script>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        return re.sub(r"\s+", " ", stripped).strip()

    return ""


def _extract_pdf_attachments(msg: email.message.Message) -> list[dict[str, Any]]:
    """Extract all PDF attachments (`application/pdf` or `.pdf` filename) from a MIME email message."""
    pdfs: list[dict[str, Any]] = []
    if not msg.is_multipart():
        return pdfs

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        content_type = (part.get_content_type() or "").lower()
        raw_filename = part.get_filename()
        filename = _decode_mime_words(raw_filename) if raw_filename else ""

        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue

        payload = part.get_payload(decode=True)
        if payload and len(payload) > 0:
            pdfs.append(
                {
                    "filename": filename or "invoice.pdf",
                    "content_type": content_type or "application/pdf",
                    "content": payload,
                }
            )
    return pdfs


def _is_potential_supplier_or_accounting_sender(
    from_email: str, suppliers: list[dict[str, Any]]
) -> bool:
    """Return True if `from_email` matches a Tier-1 supplier email, Tier-2 supplier domain,
    or Tier-3 SaaS accounting platform domain so it is exempted from `BLOCKED_SENDER_PREFIXES`.
    """
    from app.services.ap_invoice_service import (
        GENERIC_EMAIL_DOMAINS,
        is_accounting_saas_domain,
    )

    addr = (from_email or "").strip().lower()
    if not addr or "@" not in addr:
        return False

    domain = addr.split("@", 1)[1].strip().lower()
    if is_accounting_saas_domain(domain):
        return True

    for sup in suppliers:
        sup_email = str(sup.get("email_address") or "").strip().lower()
        sup_emails = [str(e).strip().lower() for e in (sup.get("email_addresses") or [])]
        if addr == sup_email or addr in sup_emails:
            return True

        if domain not in GENERIC_EMAIL_DOMAINS:
            sup_dom = str(sup.get("email_domain") or "").strip().lower()
            if sup_dom and (
                domain == sup_dom
                or domain.endswith("." + sup_dom)
                or sup_dom.endswith("." + domain)
            ):
                return True

    return False


class EmailService:
    """Asynchronous Gmail IMAP reader and SMTP sender using a Google App Password."""

    def __init__(self, fs: FirestoreService | None = None) -> None:
        self._fs = fs or firestore_service
        self._poll_lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(settings.GMAIL_ADDRESS and settings.GMAIL_APP_PASSWORD)

    async def send_email(
        self,
        to_address: str,
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Send an outbound email via Gmail SMTP (`smtp.gmail.com:587`) using the App Password."""
        if not self.is_configured:
            logger.warning("Gmail credentials not configured; skipping SMTP send to %s", to_address)
            return {"sent": False, "reason": "gmail_not_configured"}

        clean_password = settings.GMAIL_APP_PASSWORD.replace(" ", "").strip()
        sender_addr = settings.GMAIL_ADDRESS.strip()

        def _send_sync() -> dict[str, Any]:
            msg = EmailMessage()
            msg["From"] = f"LaTrobe Irrigation <{sender_addr}>"
            msg["To"] = to_address
            msg["Subject"] = subject
            msg.set_content(body)

            with smtplib.SMTP(settings.GMAIL_SMTP_HOST, settings.GMAIL_SMTP_PORT, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.login(sender_addr, clean_password)
                server.send_message(msg)

            return {"sent": True, "to": to_address, "subject": subject}

        try:
            result = await asyncio.to_thread(_send_sync)
            logger.info("Sent outbound confirmation email to %s", to_address)
            return result
        except Exception as exc:
            logger.exception("Failed to send SMTP email to %s: %s", to_address, exc)
            return {"sent": False, "error": str(exc)}

    def _fetch_unseen_from_imap(
        self, max_messages: int = 10, suppliers: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Connect to Gmail IMAP, fetch up to `max_messages` UNSEEN messages from INBOX."""
        clean_password = settings.GMAIL_APP_PASSWORD.replace(" ", "").strip()
        sender_addr = settings.GMAIL_ADDRESS.strip()
        supplier_list = suppliers or []

        fetched: list[dict[str, Any]] = []
        with imaplib.IMAP4_SSL(settings.GMAIL_IMAP_HOST, timeout=25) as mail:
            mail.login(sender_addr, clean_password)
            status, _ = mail.select("INBOX")
            if status != "OK":
                return []

            status, data = mail.uid("search", None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return []

            uids = data[0].split()
            # Process oldest-to-newest among the latest `max_messages` unseen emails
            target_uids = uids[-max_messages:]

            for uid_bytes in target_uids:
                uid_str = uid_bytes.decode("utf-8", errors="ignore")
                # Step 1: Fetch ONLY headers first (BODY.PEEK does not mark the email as read)
                status, hdr_data = mail.uid(
                    "fetch",
                    uid_bytes,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])",
                )
                if status != "OK" or not hdr_data:
                    continue

                raw_hdr_bytes: bytes | None = None
                for item in hdr_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        raw_hdr_bytes = item[1]
                        break
                if not raw_hdr_bytes:
                    continue

                parsed_hdr = email.message_from_bytes(raw_hdr_bytes)
                message_id_hdr = str(parsed_hdr.get("Message-ID") or "").strip()
                from_hdr = _decode_mime_words(parsed_hdr.get("From"))
                subject_hdr = _decode_mime_words(parsed_hdr.get("Subject"))
                display_name, from_email = parseaddr(from_hdr)
                from_email = from_email.strip().lower()

                # Skip emails sent by our own address to avoid self-loops
                if from_email == sender_addr.lower():
                    continue

                key_seed = message_id_hdr or f"uid:{uid_str}:{from_email}:{subject_hdr}"
                email_key = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:24]

                # Check if sender is a whitelisted supplier (Tier 1/2) or Accounting SaaS platform (Tier 3)
                is_supplier_or_saas = _is_potential_supplier_or_accounting_sender(
                    from_email=from_email, suppliers=supplier_list
                )

                # Step 2: Run Pre-Flight Banking / Personal / Automated Filter on headers ONLY
                # (Exempt whitelisted suppliers and Tier-3 accounting platforms so automated invoice emails aren't blocked)
                is_blocked, block_reason = is_blocked_personal_or_banking_email(
                    from_email=from_email,
                    from_name=display_name,
                    subject=subject_hdr,
                )
                if is_blocked and not is_supplier_or_saas:
                    fetched.append(
                        {
                            "uid": uid_str,
                            "email_key": email_key,
                            "message_id": message_id_hdr,
                            "from_name": display_name.strip(),
                            "from_email": from_email or from_hdr,
                            "subject": subject_hdr,
                            "body": "",
                            "pdf_attachments": [],
                            "blocked": True,
                            "block_reason": block_reason,
                            "would_be_blocked": True,
                        }
                    )
                    continue

                # Step 3: Download full message body & MIME attachments for non-blocked / supplier emails
                status, msg_data = mail.uid("fetch", uid_bytes, "(BODY.PEEK[])")
                if status != "OK" or not msg_data:
                    continue

                raw_bytes: bytes | None = None
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        raw_bytes = item[1]
                        break
                if not raw_bytes:
                    continue

                parsed_msg = email.message_from_bytes(raw_bytes)
                body_text = _extract_plain_body(parsed_msg)
                pdf_attachments = _extract_pdf_attachments(parsed_msg)

                fetched.append(
                    {
                        "uid": uid_str,
                        "email_key": email_key,
                        "message_id": message_id_hdr,
                        "from_name": display_name.strip(),
                        "from_email": from_email or from_hdr,
                        "subject": subject_hdr,
                        "body": body_text[:4000],
                        "pdf_attachments": pdf_attachments,
                        "blocked": False,
                        "block_reason": block_reason,
                        "would_be_blocked": is_blocked,
                    }
                )

        return fetched

    def _mark_uid_seen_in_imap(self, uid_str: str) -> None:
        """Mark a specific IMAP UID as \\Seen in INBOX after processing."""
        clean_password = settings.GMAIL_APP_PASSWORD.replace(" ", "").strip()
        sender_addr = settings.GMAIL_ADDRESS.strip()
        try:
            with imaplib.IMAP4_SSL(settings.GMAIL_IMAP_HOST, timeout=15) as mail:
                mail.login(sender_addr, clean_password)
                mail.select("INBOX")
                mail.uid("store", uid_str.encode("utf-8"), "+FLAGS", "(\\Seen)")
        except Exception as exc:
            logger.warning("Could not mark IMAP UID %s as Seen: %s", uid_str, exc)

    async def poll_and_process_inbox(self, max_messages: int = 10) -> dict[str, Any]:
        """Poll Gmail INBOX for unread messages, deduplicate against Firestore `processed_emails`,
        route emails with PDF attachments through the 3-Tier Supplier Invoice Extraction Pipeline,
        and route customer emails into `process_booking_request`.
        """
        if not self.is_configured:
            return {
                "ok": False,
                "skipped": True,
                "reason": "GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured",
            }

        if self._poll_lock.locked():
            return {"ok": True, "skipped": True, "reason": "poll_already_in_progress"}

        from app.services.ap_invoice_service import ap_invoice_service
        from app.services.booking_service import booking_service

        async with self._poll_lock:
            suppliers = await self._fs.list_suppliers()
            try:
                unseen_list = await asyncio.to_thread(
                    self._fetch_unseen_from_imap, max_messages, suppliers
                )
            except Exception as exc:
                logger.exception("Error polling Gmail IMAP inbox: %s", exc)
                return {"ok": False, "error": str(exc)}

            processed_results: list[dict[str, Any]] = []
            skipped_duplicates = 0
            blocked_count = 0

            for item in unseen_list:
                email_key = item["email_key"]
                uid_str = item["uid"]

                if await self._fs.is_email_processed(email_key):
                    skipped_duplicates += 1
                    continue

                # If pre-flight filter blocked this email (banking/personal/automated),
                # record it in processed_emails so we never re-check it, and DO NOT mark it Seen in Gmail!
                if item.get("blocked"):
                    blocked_count += 1
                    logger.info(
                        "Pre-flight filter skipped email from '%s' (reason=%s); leaving unread in Gmail.",
                        item["from_email"],
                        item.get("block_reason"),
                    )
                    await self._fs.mark_email_processed(
                        email_key,
                        {
                            "uid": uid_str,
                            "message_id": item["message_id"],
                            "from_email": item["from_email"],
                            "from_name": item["from_name"],
                            "subject": item["subject"],
                            "outcome": "blocked_personal_or_banking",
                            "block_reason": item.get("block_reason"),
                        },
                    )
                    continue

                pdf_attachments = item.get("pdf_attachments") or []

                # Branch A: Email contains PDF attachment(s) -> Evaluate 3-Tier Supplier Filter
                if pdf_attachments:
                    filter_res = await ap_invoice_service.evaluate_3tier_supplier_filter(
                        from_email=item["from_email"],
                        from_name=item["from_name"],
                        subject=item["subject"],
                        body_text=item["body"],
                    )
                    if filter_res.get("passed"):
                        try:
                            bill_results = await ap_invoice_service.process_supplier_invoice_email(
                                from_email=item["from_email"],
                                from_name=item["from_name"],
                                subject=item["subject"],
                                pdf_attachments=pdf_attachments,
                                matched_supplier=filter_res.get("supplier"),
                                filter_tier=filter_res.get("tier"),
                            )
                            await self._fs.mark_email_processed(
                                email_key,
                                {
                                    "uid": uid_str,
                                    "message_id": item["message_id"],
                                    "from_email": item["from_email"],
                                    "from_name": item["from_name"],
                                    "subject": item["subject"],
                                    "outcome": "supplier_invoice_pending_approval",
                                    "filter_tier": filter_res.get("tier_label"),
                                    "bills": [b.get("bill_id") for b in bill_results],
                                },
                            )
                            await asyncio.to_thread(self._mark_uid_seen_in_imap, uid_str)
                            processed_results.append(
                                {
                                    "from": item["from_email"],
                                    "subject": item["subject"],
                                    "status": "supplier_invoice_pending_approval",
                                    "filter_tier": filter_res.get("tier_label"),
                                    "bills": bill_results,
                                }
                            )
                        except Exception as ap_exc:
                            logger.exception(
                                "Error extracting supplier invoice PDF from '%s': %s",
                                item["from_email"],
                                ap_exc,
                            )
                            processed_results.append(
                                {
                                    "from": item["from_email"],
                                    "subject": item["subject"],
                                    "status": "supplier_invoice_error",
                                    "error": str(ap_exc),
                                }
                            )
                        continue

                # If the email was only exempted from the personal/banking blocklist to check for a supplier PDF,
                # and it either had no PDF or failed the 3-tier supplier filter, block/ignore it now.
                if item.get("would_be_blocked"):
                    blocked_count += 1
                    await self._fs.mark_email_processed(
                        email_key,
                        {
                            "uid": uid_str,
                            "message_id": item["message_id"],
                            "from_email": item["from_email"],
                            "from_name": item["from_name"],
                            "subject": item["subject"],
                            "outcome": "blocked_personal_or_banking",
                            "block_reason": item.get("block_reason"),
                        },
                    )
                    continue

                # Branch B: Standard inbound customer booking pipeline
                sender_identifier = item["from_email"] or item["from_name"]
                combined_text = (
                    f"Subject: {item['subject']}\n"
                    f"From: {item['from_name']} <{item['from_email']}>\n\n"
                    f"{item['body']}"
                ).strip()

                inbound_msg = InboundMessage(
                    source="email",
                    sender_id=sender_identifier,
                    text=combined_text,
                )

                res = await booking_service.process_booking_request(inbound_msg)

                await self._fs.mark_email_processed(
                    email_key,
                    {
                        "uid": uid_str,
                        "message_id": item["message_id"],
                        "from_email": item["from_email"],
                        "from_name": item["from_name"],
                        "subject": item["subject"],
                        "outcome": res.get("status"),
                        "proposal_id": res.get("proposal_id"),
                    },
                )
                if res.get("status") == "proposal_sent":
                    await asyncio.to_thread(self._mark_uid_seen_in_imap, uid_str)
                processed_results.append(
                    {
                        "from": item["from_email"],
                        "subject": item["subject"],
                        "status": res.get("status"),
                        "proposal_id": res.get("proposal_id"),
                    }
                )

            return {
                "ok": True,
                "unseen_fetched": len(unseen_list),
                "processed_count": len(processed_results),
                "blocked_count": blocked_count,
                "skipped_duplicates": skipped_duplicates,
                "results": processed_results,
            }


email_service = EmailService()
