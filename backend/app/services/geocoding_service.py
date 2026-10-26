import asyncio
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "LaTrobe-Irrigation-Operations/1.0 (info@latrobeirrigation.com.au)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def clean_address_string(addr: dict[str, Any] | None) -> tuple[str, str, str, str]:
    """Extract and normalize address components from a QuickBooks address dictionary.

    Returns:
        tuple of (line1_cleaned, city_normalized, state, postal_code)
    """
    if not isinstance(addr, dict):
        return "", "", "", ""

    raw_line1 = str(addr.get("Line1") or "").strip()
    raw_city = str(addr.get("City") or "").strip()
    raw_state = str(addr.get("CountrySubDivisionCode") or "Victoria").strip()
    raw_postal = str(addr.get("PostalCode") or "").strip()

    # Ignore PO Box lines for physical coordinates
    line1 = ""
    if raw_line1 and not re.search(r"P\.?O\.?\s*Box", raw_line1, flags=re.IGNORECASE):
        line1 = raw_line1

    # Normalize known Victoria suburb variations
    city = raw_city
    if re.search(r"^red\s*hill$", city, flags=re.IGNORECASE) or re.search(r"^redhill$", city, flags=re.IGNORECASE):
        city = "Red Hill"

    state = "Victoria" if raw_state.lower() in ("vic", "victoria", "") else raw_state

    return line1, city, state, raw_postal


def format_display_address(addr: dict[str, Any] | None) -> str:
    """Format a clean, readable one-line address for UI display."""
    if not isinstance(addr, dict):
        return ""

    parts = [
        str(addr.get("Line1") or "").strip(),
        str(addr.get("Line2") or "").strip(),
        str(addr.get("City") or "").strip(),
        str(addr.get("CountrySubDivisionCode") or "VIC").strip(),
        str(addr.get("PostalCode") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


async def geocode_address_dict(
    addr: dict[str, Any] | None,
    timeout: float = 8.0,
) -> tuple[float | None, float | None, str]:
    """Convert a QuickBooks address dictionary into latitude and longitude coordinates.

    1. Checks if QuickBooks already has valid 'Lat' and 'Long' values.
    2. Falls back to OpenStreetMap Nominatim geocoding.
    3. If full street query fails, falls back to suburb-level coordinates.

    Returns:
        tuple of (lat, lng, display_address)
    """
    if not isinstance(addr, dict):
        return None, None, ""

    display_address = format_display_address(addr)

    # 1. Check if QuickBooks already has valid Lat and Long
    raw_lat = addr.get("Lat")
    raw_lng = addr.get("Long")
    if raw_lat and raw_lng and str(raw_lat).upper() != "INVALID" and str(raw_lng).upper() != "INVALID":
        try:
            return float(raw_lat), float(raw_lng), display_address
        except (ValueError, TypeError):
            pass

    line1, city, state, postal = clean_address_string(addr)
    if not city and not line1:
        return None, None, display_address

    # 2. Build geocoding query candidates (precise first, suburb fallback second)
    candidates: list[str] = []

    if line1 and city:
        query_parts = [line1, city, state]
        if postal:
            query_parts.append(postal)
        query_parts.append("Australia")
        candidates.append(", ".join(query_parts))

    if city:
        suburb_parts = [city, state]
        if postal:
            suburb_parts.append(postal)
        suburb_parts.append("Australia")
        candidates.append(", ".join(suburb_parts))

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for candidate in candidates:
                try:
                    response = await client.get(
                        NOMINATIM_URL,
                        params={"q": candidate, "format": "json", "limit": 1},
                        headers=headers,
                    )
                    if response.status_code == 200:
                        results = response.json()
                        if results and len(results) > 0:
                            item = results[0]
                            lat = float(item["lat"])
                            lng = float(item["lon"])
                            logger.info(
                                "Geocoded '%s' -> (%f, %f)", candidate, lat, lng
                            )
                            return lat, lng, display_address
                except Exception as query_exc:
                    logger.debug("Candidate geocode failed for '%s': %s", candidate, query_exc)
    except Exception as exc:
        logger.warning("Geocoding network request failed for '%s': %s", display_address, exc)

    return None, None, display_address
