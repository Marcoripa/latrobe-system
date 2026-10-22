import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.firestore_service import FirestoreService, firestore_service

logger = logging.getLogger(__name__)


class QuickBooksAPIError(Exception):
    """Raised when QuickBooks Online API returns an error or authentication fails."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class QuickBooksService:
    """Async client for Intuit QuickBooks Online REST API v3 with automatic token refresh."""

    TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    SANDBOX_BASE_URL = "https://sandbox-quickbooks.api.intuit.com"
    PRODUCTION_BASE_URL = "https://quickbooks.api.intuit.com"
    MINOR_VERSION = "73"

    def __init__(
        self,
        fs_service: FirestoreService | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._firestore = fs_service or firestore_service
        self._timeout = timeout
        self._access_token: str = settings.QUICKBOOKS_ACCESS_TOKEN
        self._refresh_token: str = settings.QUICKBOOKS_REFRESH_TOKEN
        self._realm_id: str = settings.QUICKBOOKS_REALM_ID
        self._token_lock = asyncio.Lock()
        self._tokens_loaded_from_store = False

    @property
    def base_url(self) -> str:
        """Return the Intuit API base URL based on configured environment."""
        if settings.QUICKBOOKS_ENVIRONMENT == "production":
            return self.PRODUCTION_BASE_URL
        return self.SANDBOX_BASE_URL

    def get_invoice_review_url(self, qbo_invoice_id: str) -> str:
        """Return the QuickBooks Online UI URL where a manager can review the draft invoice."""
        if settings.QUICKBOOKS_ENVIRONMENT == "production":
            return f"https://app.qbo.intuit.com/app/invoice?txnId={qbo_invoice_id}"
        return f"https://app.sandbox.qbo.intuit.com/app/invoice?txnId={qbo_invoice_id}"

    async def _ensure_tokens_loaded(self) -> None:
        """Load the latest persisted tokens from Firestore if available."""
        if self._tokens_loaded_from_store:
            return
        stored = await self._firestore.get_qbo_tokens()
        if stored:
            self._access_token = stored.get("access_token") or self._access_token
            self._refresh_token = stored.get("refresh_token") or self._refresh_token
        self._tokens_loaded_from_store = True

    async def refresh_access_token(self) -> str:
        """Refresh the OAuth2 access token using the stored refresh token and persist new tokens."""
        async with self._token_lock:
            await self._ensure_tokens_loaded()

            if not settings.QUICKBOOKS_CLIENT_ID or not settings.QUICKBOOKS_CLIENT_SECRET:
                raise QuickBooksAPIError(
                    "QuickBooks client credentials (QUICKBOOKS_CLIENT_ID / QUICKBOOKS_CLIENT_SECRET) are not configured."
                )
            if not self._refresh_token:
                raise QuickBooksAPIError(
                    "QuickBooks refresh token is missing. Cannot refresh access token."
                )

            raw_creds = f"{settings.QUICKBOOKS_CLIENT_ID}:{settings.QUICKBOOKS_CLIENT_SECRET}"
            basic_auth = base64.b64encode(raw_creds.encode("utf-8")).decode("utf-8")

            headers = {
                "Authorization": f"Basic {basic_auth}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self.TOKEN_URL, headers=headers, data=data
                    )
            except httpx.RequestError as exc:
                logger.error("Network error while refreshing QuickBooks token: %s", exc)
                raise QuickBooksAPIError(
                    f"Network error refreshing QuickBooks token: {exc}"
                ) from exc

            if response.status_code != 200:
                body = self._safe_json(response)
                logger.error(
                    "QuickBooks OAuth2 token refresh failed (HTTP %d): %s",
                    response.status_code,
                    body,
                )
                raise QuickBooksAPIError(
                    f"Failed to refresh QuickBooks token (HTTP {response.status_code})",
                    status_code=response.status_code,
                    response_body=body,
                )

            payload = response.json()
            new_access_token = payload.get("access_token")
            new_refresh_token = payload.get("refresh_token", self._refresh_token)
            expires_in = int(payload.get("expires_in", 3600))

            if not new_access_token:
                raise QuickBooksAPIError(
                    "QuickBooks token response did not include an access_token."
                )

            self._access_token = new_access_token
            self._refresh_token = new_refresh_token

            await self._firestore.save_qbo_tokens(
                access_token=self._access_token,
                refresh_token=self._refresh_token,
                expires_in=expires_in,
            )
            logger.info("Successfully refreshed QuickBooks Online OAuth2 access token.")
            return self._access_token

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated request against QuickBooks API v3 with automatic 401 retry."""
        await self._ensure_tokens_loaded()

        if not self._realm_id:
            raise QuickBooksAPIError("QUICKBOOKS_REALM_ID is not configured.")

        if not self._access_token and self._refresh_token:
            await self.refresh_access_token()

        url = f"{self.base_url}{endpoint}"
        query_params = {"minorversion": self.MINOR_VERSION}
        if params:
            query_params.update(params)

        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=json_payload,
                        params=query_params,
                    )
            except httpx.RequestError as exc:
                logger.error("QuickBooks API request error (%s %s): %s", method, url, exc)
                raise QuickBooksAPIError(
                    f"Network error communicating with QuickBooks API: {exc}"
                ) from exc

            if response.status_code == 401 and attempt == 0:
                logger.info(
                    "QuickBooks access token expired (HTTP 401). Refreshing token and retrying..."
                )
                await self.refresh_access_token()
                continue

            if response.status_code >= 400:
                error_body = self._safe_json(response)
                logger.error(
                    "QuickBooks API error (HTTP %d on %s %s): %s",
                    response.status_code,
                    method,
                    endpoint,
                    error_body,
                )
                raise QuickBooksAPIError(
                    f"QuickBooks API returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    response_body=error_body,
                )

            return response.json()

        raise QuickBooksAPIError("Exhausted retries for QuickBooks API request.")

    @staticmethod
    def _format_qbo_line_item(raw_item: dict[str, Any], index: int) -> dict[str, Any]:
        """Normalize either a raw QBO Line dict or simplified line item dict into QBO SalesItemLineDetail format."""
        if "DetailType" in raw_item and "Amount" in raw_item:
            return raw_item

        quantity = float(raw_item.get("quantity", 1.0))
        unit_price = float(raw_item.get("unit_price", 0.0))
        amount = raw_item.get("amount")
        computed_amount = (
            round(float(amount), 2)
            if amount is not None
            else round(quantity * unit_price, 2)
        )

        item_ref_id = str(raw_item.get("item_ref_id") or raw_item.get("item_id") or "1")
        item_ref: dict[str, str] = {"value": item_ref_id}
        if raw_item.get("item_ref_name") or raw_item.get("name"):
            item_ref["name"] = str(raw_item.get("item_ref_name") or raw_item.get("name"))

        sales_detail: dict[str, Any] = {
            "ItemRef": item_ref,
            "Qty": quantity,
            "UnitPrice": unit_price,
        }
        if raw_item.get("tax_code_ref"):
            sales_detail["TaxCodeRef"] = {"value": str(raw_item["tax_code_ref"])}

        return {
            "LineNum": index,
            "Description": str(raw_item.get("description") or raw_item.get("name") or "Irrigation Service / Material"),
            "Amount": computed_amount,
            "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": sales_detail,
        }

    async def create_draft_invoice(
        self,
        client_id: str,
        line_items: list[dict[str, Any]],
        private_note: str = "",
    ) -> dict[str, Any]:
        """Construct and send a POST request to QuickBooks `/v3/company/{realm_id}/invoice`
        creating a DRAFT invoice (`EmailStatus="NeedToSend"` so it does NOT auto-email the client).
        """
        if not client_id:
            raise ValueError("client_id (QuickBooks CustomerRef) is required.")
        if not line_items:
            raise ValueError("At least one line item is required to create a draft invoice.")

        formatted_lines = [
            self._format_qbo_line_item(item, idx)
            for idx, item in enumerate(line_items, start=1)
        ]

        invoice_payload: dict[str, Any] = {
            "CustomerRef": {"value": str(client_id)},
            "Line": formatted_lines,
            "EmailStatus": "NeedToSend",
            "PrintStatus": "NeedToPrint",
            "TxnDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

        if private_note:
            invoice_payload["PrivateNote"] = private_note[:4000]

        endpoint = f"/v3/company/{self._realm_id}/invoice"
        response_data = await self._request(
            "POST", endpoint, json_payload=invoice_payload
        )

        invoice_obj = response_data.get("Invoice", response_data)
        qbo_id = str(invoice_obj.get("Id", ""))
        logger.info(
            "Created QuickBooks draft invoice Id='%s' (DocNumber='%s') for CustomerRef='%s'",
            qbo_id,
            invoice_obj.get("DocNumber"),
            client_id,
        )
        return invoice_obj

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text


quickbooks_service = QuickBooksService()
