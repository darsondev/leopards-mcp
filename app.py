"""Leopards Courier (LCS) MCP server -- Vercel entrypoint.

Exposes the Leopards Courier merchant API (API Documentation v2.0) as MCP
tools so Claude can book, track, cancel and reconcile COD shipments.

Vercel's Python runtime looks for a FastAPI instance named `app`; the MCP
endpoint is `/mcp`.

Credentials
-----------
Leopards needs two values on every call: an API key and an API password.
This server accepts them in either of two ways:

1. Stored on the server (recommended). Set three environment variables in
   Vercel:
       LCS_API_KEY        your Leopards API key
       LCS_API_PASSWORD   your Leopards API password
       MCP_ACCESS_KEY     a long random secret that you invent
   and connect with   https://<project>.vercel.app/mcp?key=<MCP_ACCESS_KEY>
   The stored credentials are used ONLY when the request carries the matching
   access key, so a stranger who finds the address cannot use your account.

2. Sent with each request (nothing stored):
       https://<project>.vercel.app/mcp?api_key=<key>&api_password=<password>
   or the headers  x-lcs-api-key  /  x-lcs-api-password.

Credentials are forwarded to Leopards only. They are never logged and never
appear in error messages.
"""

from __future__ import annotations

import base64
import hmac
import os
from typing import Annotated, Any

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers, get_http_request
from pydantic import Field

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URLS = {
    "production": "https://merchantapi.leopardscourier.com/api",
    "staging": "https://merchantapistaging.leopardscourier.com/api",
}

ENV_API_KEY = os.environ.get("LCS_API_KEY", "").strip()
ENV_API_PASSWORD = os.environ.get("LCS_API_PASSWORD", "").strip()
ACCESS_KEY = os.environ.get("MCP_ACCESS_KEY", "").strip()
DEFAULT_ENV = os.environ.get("LCS_ENV", "production").strip().lower() or "production"
REQUEST_TIMEOUT = float(os.environ.get("LCS_TIMEOUT", "45"))

# Leopards caps these lookups at 50 consignment numbers per call.
MAX_CN_PER_CALL = 50

INSTRUCTIONS = """
Tools for Leopards Courier (LCS), a Pakistani cash-on-delivery courier,
covering the merchant API: cities, booking, tracking, cancellation, load
sheets, shipper advice, COD payment status, shipping charges and invoices.

Notes for callers:
- A consignment number (CN / tracking number) is what Leopards returns when a
  packet is booked, e.g. KI123456789.
- Cities are numeric IDs. Call `get_cities` with a `search` term to find the
  ID before booking or asking for a tariff. Never guess a city ID.
- Weight is in GRAMS (2 kg = 2000).
- Dates are `yyyy-mm-dd`.
- Phone numbers use the local `03xxxxxxxxx` format.
- `book_packet`, `cancel_packets`, `generate_load_sheet` and
  `update_shipper_advice` change real shipments and real money. Show the user
  exactly what will be sent and get a clear yes before calling them.
""".strip()

mcp = FastMCP(name="leopards-courier", instructions=INSTRUCTIONS, version="1.0.0")


class LeopardsError(RuntimeError):
    """Raised when Leopards rejects a request or returns an unusable response."""


NO_CREDENTIALS_HELP = """
No Leopards Courier credentials were supplied with this request.

Connect in one of these ways:

  1. Server-stored credentials: set LCS_API_KEY, LCS_API_PASSWORD and
     MCP_ACCESS_KEY in the Vercel project, then use
        https://<project>.vercel.app/mcp?key=<MCP_ACCESS_KEY>

  2. Per-request credentials:
        https://<project>.vercel.app/mcp?api_key=<key>&api_password=<password>

Get the API key and password from the Leopards merchant portal, under
API Settings > API Management.
""".strip()

BAD_ACCESS_KEY_HELP = (
    "The access key on this connector address does not match the "
    "MCP_ACCESS_KEY set in the Vercel project. Fix the `?key=` part of the "
    "connector address, or update MCP_ACCESS_KEY and redeploy."
)


# --------------------------------------------------------------------------
# Credentials and environment
# --------------------------------------------------------------------------
def _query(name: str) -> str:
    try:
        return (get_http_request().query_params.get(name) or "").strip()
    except RuntimeError:
        return ""


def _credentials() -> tuple[str, str]:
    """Resolve the Leopards API key and password for this request.

    Read fresh on every request and forwarded to Leopards as-is; never
    stored, cached or logged.
    """
    headers = get_http_headers(include={"authorization"}) or {}

    # 1. Credentials sent with the request.
    key = (headers.get("x-lcs-api-key") or "").strip() or _query("api_key")
    password = (headers.get("x-lcs-api-password") or "").strip() or _query(
        "api_password"
    )
    if key and password:
        return key, password

    # 2. Credentials stored on the server, unlocked by the access key.
    if ENV_API_KEY and ENV_API_PASSWORD:
        if not ACCESS_KEY:
            raise ValueError(
                "LCS_API_KEY and LCS_API_PASSWORD are set on the server but "
                "MCP_ACCESS_KEY is not. For safety the stored credentials "
                "stay locked until MCP_ACCESS_KEY is set in the Vercel "
                "project and the connector address ends with ?key=<that value>."
            )
        supplied = _query("key") or (headers.get("x-mcp-access-key") or "").strip()
        authorization = headers.get("authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if supplied and hmac.compare_digest(supplied.encode(), ACCESS_KEY.encode()):
            return ENV_API_KEY, ENV_API_PASSWORD
        raise ValueError(BAD_ACCESS_KEY_HELP if supplied else NO_CREDENTIALS_HELP)

    raise ValueError(NO_CREDENTIALS_HELP)


def _base_url() -> str:
    headers = get_http_headers() or {}
    env = (
        _query("env") or (headers.get("x-lcs-env") or "").strip() or DEFAULT_ENV
    ).lower()
    if env not in BASE_URLS:
        raise ValueError(
            f"Unknown environment {env!r}; use 'production' or 'staging'."
        )
    return BASE_URLS[env]


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the caller left unset so Leopards applies its own defaults."""
    return {k: v for k, v in data.items() if v is not None}


def _is_ok(status: Any) -> bool:
    return str(status).strip() == "1"


async def _send(
    method: str,
    endpoint: str,
    data: dict[str, Any] | None = None,
    *,
    json_suffix: bool = True,
) -> httpx.Response:
    """Send one request to Leopards and return the raw response.

    `method` follows the guide: POST endpoints take a JSON body, GET endpoints
    take a query string. Credentials are added here and nowhere else.
    """
    api_key, api_password = _credentials()
    url = f"{_base_url()}/{endpoint}/" + ("format/json/" if json_suffix else "")
    payload = {"api_key": api_key, "api_password": api_password, **_clean(data or {})}

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            if method == "GET":
                return await client.get(url, params=payload)
            return await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        # Deliberately not str(exc): for GET calls it can contain the full
        # URL, which carries the credentials in its query string.
        raise LeopardsError(
            f"Could not reach Leopards ({endpoint}): {type(exc).__name__}. "
            "The Leopards server may be slow or down; try again shortly."
        ) from None


def _parse(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise LeopardsError(
            f"Leopards returned a non-JSON response from {endpoint} "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        ) from None

    if not isinstance(payload, dict):
        return {"status": 1, "data": payload}

    if not _is_ok(payload.get("status")):
        error = payload.get("error")
        raise LeopardsError(
            f"Leopards rejected the {endpoint} request "
            f"(HTTP {response.status_code}): {error if error else payload}"
        )
    return payload


async def _call(
    method: str, endpoint: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        return _parse(await _send(method, endpoint, data), endpoint)
    except LeopardsError as exc:
        # Surface Leopards' own wording to the model rather than a stack trace.
        raise ValueError(str(exc)) from None


def _cn_list(values: list[str], *, limit: int | None = None) -> list[str]:
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if not cleaned:
        raise ValueError("Provide at least one consignment (CN) number.")
    if limit and len(cleaned) > limit:
        raise ValueError(
            f"Leopards accepts at most {limit} CN numbers per call; "
            f"got {len(cleaned)}. Split them into smaller batches."
        )
    return cleaned


# --------------------------------------------------------------------------
# Cities
# --------------------------------------------------------------------------
@mcp.tool
async def get_cities(
    search: Annotated[
        str | None,
        Field(
            description=(
                "Part of a city name to filter by, e.g. 'lahore'. Strongly "
                "recommended: the full list is very long."
            )
        ),
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum number of cities to return.", ge=1, le=5000)
    ] = 50,
) -> dict:
    """Find Leopards city IDs, with origin/destination availability.

    Call this before `book_packet` or `get_tariff` to get the numeric city ID
    Leopards expects. Each city lists its `id`, `name`, the shipment types it
    supports, and whether it can be used as an origin or a destination.
    """
    payload = await _call("POST", "getAllCities")
    cities = payload.get("city_list") or []
    total = len(cities)
    if search:
        needle = search.strip().lower()
        cities = [c for c in cities if needle in str(c.get("name", "")).lower()]
    return {
        "status": 1,
        "total_cities": total,
        "matched": len(cities),
        "returned": min(len(cities), limit),
        "city_list": cities[:limit],
    }


# --------------------------------------------------------------------------
# Booking
# --------------------------------------------------------------------------
@mcp.tool
async def book_packet(
    weight_grams: Annotated[
        int, Field(description="Packet weight in GRAMS, e.g. 2000 for 2 kg.", gt=0)
    ],
    pieces: Annotated[int, Field(description="Number of pieces.", ge=1)],
    cod_amount: Annotated[
        int,
        Field(
            description="Cash to collect on delivery, in PKR. Use 0 for a prepaid packet.",
            ge=0,
        ),
    ],
    destination_city: Annotated[
        str,
        Field(description="Destination city ID from `get_cities`, e.g. '789'."),
    ],
    consignee_name: Annotated[str, Field(description="Customer (receiver) name.")],
    consignee_phone: Annotated[
        str, Field(description="Customer phone, e.g. 03001234567.")
    ],
    consignee_address: Annotated[str, Field(description="Full delivery address.")],
    special_instructions: Annotated[
        str,
        Field(
            description=(
                "Required by Leopards. Handling notes or a short description "
                "of the contents, e.g. 'Cutlery set - handle with care'."
            )
        ),
    ],
    order_id: Annotated[
        str | None, Field(description="Your own order reference, e.g. Shopify #1042.")
    ] = None,
    origin_city: Annotated[
        str,
        Field(
            description="'self' to use the account's own city, or a city ID from `get_cities`."
        ),
    ] = "self",
    shipper_name: Annotated[
        str, Field(description="'self' for the account's company name, or another name.")
    ] = "self",
    shipper_email: Annotated[
        str, Field(description="'self' for the account's email, or another email.")
    ] = "self",
    shipper_phone: Annotated[
        str, Field(description="'self' for the account's phone, or another phone.")
    ] = "self",
    shipper_address: Annotated[
        str, Field(description="'self' for the account's address, or another address.")
    ] = "self",
    shipment_id: Annotated[
        int | None,
        Field(description="Optional shipper/sub-account ID, if the account has several."),
    ] = None,
    consignee_email: Annotated[str | None, Field(description="Optional.")] = None,
    consignee_phone_two: Annotated[str | None, Field(description="Optional.")] = None,
    consignee_phone_three: Annotated[str | None, Field(description="Optional.")] = None,
    shipment_type: Annotated[
        str | None,
        Field(
            description=(
                "Optional shipment type name; one of the `shipment_type` "
                "values `get_cities` lists for the destination. Leopards "
                "defaults to 'overnight' when omitted."
            )
        ),
    ] = None,
    vol_weight_w: Annotated[
        int | None, Field(description="Optional volumetric width.")
    ] = None,
    vol_weight_h: Annotated[
        int | None, Field(description="Optional volumetric height.")
    ] = None,
    vol_weight_l: Annotated[
        int | None, Field(description="Optional volumetric length.")
    ] = None,
    return_address: Annotated[
        str | None,
        Field(description="Optional. Defaults to the shipper address when omitted."),
    ] = None,
    return_city: Annotated[
        int | None,
        Field(description="Optional return city ID. Defaults to the origin city."),
    ] = None,
    custom_data: Annotated[
        list[dict] | None,
        Field(description="Optional list of key/value objects to store with the packet."),
    ] = None,
    is_vpc: Annotated[
        int | None,
        Field(
            description=(
                "Optional. Set 1 for a VPC (reverse) booking; `order_id` must "
                "then be the original CN number."
            )
        ),
    ] = None,
) -> dict:
    """Book ONE packet with Leopards. Returns the CN number and a slip link.

    THIS CREATES A REAL SHIPMENT. Before calling, show the user the consignee,
    address, city, weight and COD amount and get a clear yes. To book several
    orders, call this once per order.

    The response carries `track_number` (the CN) and `slip_link` (open it in a
    browser to print the shipping slip).
    """
    return await _call(
        "POST",
        "bookPacket",
        {
            "booked_packet_weight": weight_grams,
            "booked_packet_vol_weight_w": vol_weight_w,
            "booked_packet_vol_weight_h": vol_weight_h,
            "booked_packet_vol_weight_l": vol_weight_l,
            "booked_packet_no_piece": pieces,
            "booked_packet_collect_amount": cod_amount,
            "booked_packet_order_id": order_id,
            "origin_city": str(origin_city).strip(),
            "destination_city": str(destination_city).strip(),
            "shipment_id": shipment_id,
            "shipment_name_eng": shipper_name,
            "shipment_email": shipper_email,
            "shipment_phone": shipper_phone,
            "shipment_address": shipper_address,
            "consignment_name_eng": consignee_name,
            "consignment_email": consignee_email,
            "consignment_phone": consignee_phone,
            "consignment_phone_two": consignee_phone_two,
            "consignment_phone_three": consignee_phone_three,
            "consignment_address": consignee_address,
            "special_instructions": special_instructions,
            "shipment_type": shipment_type,
            "custom_data": custom_data,
            "return_address": return_address,
            "return_city": return_city,
            "is_vpc": is_vpc,
        },
    )


@mcp.tool
async def cancel_packets(
    cn_numbers: Annotated[
        list[str], Field(description="CN numbers of the booked packets to cancel.")
    ],
) -> dict:
    """Cancel one or more booked packets.

    THIS CANCELS REAL SHIPMENTS. Confirm the exact CN numbers with the user
    before calling.
    """
    return await _call(
        "POST", "cancelBookedPackets", {"cn_numbers": ",".join(_cn_list(cn_numbers))}
    )


# --------------------------------------------------------------------------
# Tracking and lookups
# --------------------------------------------------------------------------
@mcp.tool
async def track_packets(
    cn_numbers: Annotated[
        list[str], Field(description="One or more CN (tracking) numbers.")
    ],
) -> dict:
    """Track one or more packets: current status plus the full journey.

    Each packet in `packet_list` has `booked_packet_status`, consignee and COD
    details, and a `Tracking Detail` list of status events with dates.
    """
    return await _call(
        "POST", "trackBookedPacket", {"track_numbers": ",".join(_cn_list(cn_numbers))}
    )


@mcp.tool
async def list_packets_by_date(
    from_date: Annotated[str, Field(description="Start booking date, yyyy-mm-dd.")],
    to_date: Annotated[str, Field(description="End booking date, yyyy-mm-dd.")],
) -> dict:
    """List packets booked in a date range with each one's latest status.

    Good for daily reviews and COD reconciliation: each row has the tracking
    number, order ID, consignee, cities, `booked_packet_status`,
    `delivery_date` and `cod_value`. Keep ranges short (a few days to a
    month); long ranges return a lot of rows and may be slow.
    """
    return await _call(
        "GET", "getBookedPacketLastStatus", {"from_date": from_date, "to_date": to_date}
    )


@mcp.tool
async def get_shipments_by_order_id(
    order_ids: Annotated[
        list[str],
        Field(description="Your own order references, as sent when booking."),
    ],
) -> dict:
    """Look up shipments by your own order ID instead of the CN number.

    Returns the CN number, status, delivery/return dates and invoice details
    for each order ID.
    """
    cleaned = [str(o).strip() for o in order_ids if str(o).strip()]
    if not cleaned:
        raise ValueError("Provide at least one order ID.")
    return await _call(
        "POST", "getShipmentDetailsByOrderID", {"shipment_order_id": cleaned}
    )


@mcp.tool
async def get_proof_of_delivery(
    cn_numbers: Annotated[
        list[str], Field(description="CN numbers (max 50 per call).")
    ],
) -> dict:
    """Get the electronic proof of delivery for delivered packets.

    Includes receiver name and relation, courier, delivery location
    coordinates and a signature image link (`Sig_Url`).
    """
    cns = _cn_list(cn_numbers, limit=MAX_CN_PER_CALL)
    return await _call(
        "GET", "getElectronicProofOfDelivery", {"cn_number": ",".join(cns)}
    )


# --------------------------------------------------------------------------
# Load sheets
# --------------------------------------------------------------------------
@mcp.tool
async def generate_load_sheet(
    cn_numbers: Annotated[
        list[str], Field(description="CN numbers to include in the pickup load sheet.")
    ],
    courier_name: Annotated[
        str | None, Field(description="Optional name of the pickup rider.")
    ] = None,
    courier_code: Annotated[
        str | None, Field(description="Optional code of the pickup rider.")
    ] = None,
) -> dict:
    """Generate a pickup load sheet for booked packets. Returns `load_sheet_id`.

    This records a handover list in Leopards' system; confirm the CN numbers
    with the user first. Pass the returned ID to `download_load_sheet`.
    """
    return await _call(
        "POST",
        "generateLoadSheet",
        {
            "cn_numbers": _cn_list(cn_numbers),
            "courier_name": courier_name,
            "courier_code": courier_code,
        },
    )


@mcp.tool
async def download_load_sheet(
    load_sheet_id: Annotated[
        int, Field(description="ID returned by `generate_load_sheet`.")
    ],
    as_pdf: Annotated[
        bool,
        Field(
            description=(
                "False (default) returns the packets on the sheet as data. "
                "True returns the printable PDF, base64-encoded."
            )
        ),
    ] = False,
) -> dict:
    """Fetch a load sheet, either as packet data or as a printable PDF."""
    body = {"load_sheet_id": load_sheet_id, "response_type": "PDF" if as_pdf else "JSON"}
    if not as_pdf:
        return await _call("POST", "downloadLoadSheet", body)

    try:
        response = await _send("POST", "downloadLoadSheet", body, json_suffix=False)
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
            # Failures come back as the usual JSON envelope.
            return _parse(response, "downloadLoadSheet")
    except LeopardsError as exc:
        raise ValueError(str(exc)) from None

    return {
        "status": 1,
        "contentType": "application/pdf",
        "sizeBytes": len(response.content),
        "pdfBase64": base64.b64encode(response.content).decode("ascii"),
    }


# --------------------------------------------------------------------------
# Money: payments, charges, tariff, invoices
# --------------------------------------------------------------------------
@mcp.tool
async def get_payment_details(
    cn_numbers: Annotated[
        list[str], Field(description="CN numbers (max 50 per call).")
    ],
) -> dict:
    """Check COD payment status for packets: paid or not, and how.

    Each entry has `status`, `billing_method`, `payment_method`,
    `invoice_cheque_no`, `invoice_cheque_date` and a `slip_link`.
    """
    cns = _cn_list(cn_numbers, limit=MAX_CN_PER_CALL)
    return await _call("GET", "getPaymentDetails", {"cn_numbers": ",".join(cns)})


@mcp.tool
async def get_shipping_charges(
    cn_numbers: Annotated[
        list[str], Field(description="CN numbers (max 50 per call).")
    ],
) -> dict:
    """Get what Leopards actually charged for packets.

    Shows charged weight, shipment, cash-handling, return, insurance and fuel
    charges, GST, the COD amount and net/gross charges per CN.
    """
    cns = _cn_list(cn_numbers, limit=MAX_CN_PER_CALL)
    return await _call("GET", "getShippingCharges", {"cn_numbers": ",".join(cns)})


@mcp.tool
async def get_tariff(
    weight_grams: Annotated[
        int, Field(description="Packet weight in GRAMS.", gt=0)
    ],
    origin_city: Annotated[
        int, Field(description="Origin city ID from `get_cities`.")
    ],
    destination_city: Annotated[
        int, Field(description="Destination city ID from `get_cities`.")
    ],
    shipment_type: Annotated[
        str,
        Field(
            description=(
                "Shipment type ID, as Leopards defines it for your account. "
                "The guide does not list the IDs; if a value is rejected, ask "
                "Leopards for your account's shipment type IDs."
            )
        ),
    ],
    cod_amount: Annotated[
        int,
        Field(
            description="COD amount in PKR, or 0 for prepaid. Affects cash-handling charges.",
            ge=0,
        ),
    ] = 0,
) -> dict:
    """Estimate the delivery charges for a packet before booking it.

    Returns shipment charges, cash handling, insurance, GST and fuel
    surcharge.
    """
    return await _call(
        "GET",
        "getTariffDetails",
        {
            "packet_weight": weight_grams,
            "shipment_type": shipment_type,
            "origin_city": origin_city,
            "destination_city": destination_city,
            "cod_amount": cod_amount,
        },
    )


@mcp.tool
async def list_invoices() -> dict:
    """List the account's COD invoices / payment cheques.

    Each has the invoice or cheque number, date, holder name, amount, bank,
    payment method and paid status.
    """
    return await _call("GET", "getInvoices")


# --------------------------------------------------------------------------
# Shipper advice (what to do with a packet that could not be delivered)
# --------------------------------------------------------------------------
def _advice_filters(
    from_date: str | None,
    to_date: str | None,
    cn_number: str | None,
    status: str | None,
    product: str | None,
    origin_city: int | None,
    destination_city: int | None,
    start: int,
    length: int,
) -> dict[str, Any]:
    """Build the filter body shared by the advice list and the activity log.

    The guide documents this endpoint twice with different field names, so
    both spellings of the date and city filters are sent.
    """
    origin = "" if origin_city is None else origin_city
    destination = "" if destination_city is None else destination_city
    return {
        "product": product or "",
        "status": status or "",
        "origionID": origin,
        "destinationID": destination,
        "dateFrom": from_date or "",
        "toDate": to_date or "",
        "Cn_number": cn_number or "",
        "start": start,
        "length": length,
        # Older spelling from the same guide.
        "from_date": from_date,
        "to_date": to_date,
        "origin_city": origin_city,
        "destination_city": destination_city,
    }


@mcp.tool
async def list_shipper_advice(
    from_date: Annotated[
        str | None, Field(description="Start date, yyyy-mm-dd.")
    ] = None,
    to_date: Annotated[str | None, Field(description="End date, yyyy-mm-dd.")] = None,
    cn_number: Annotated[
        str | None, Field(description="Limit to one CN number.")
    ] = None,
    status: Annotated[
        str | None, Field(description="Optional status filter, as Leopards names it.")
    ] = None,
    product: Annotated[
        str | None, Field(description="Optional product filter, as Leopards names it.")
    ] = None,
    origin_city: Annotated[
        int | None, Field(description="Optional origin city ID.")
    ] = None,
    destination_city: Annotated[
        int | None, Field(description="Optional destination city ID.")
    ] = None,
    start: Annotated[int, Field(description="Row offset for paging.", ge=0)] = 0,
    length: Annotated[
        int, Field(description="Rows per page.", ge=1, le=500)
    ] = 100,
) -> dict:
    """List packets awaiting shipper advice (failed delivery attempts).

    Each row has an `id`, the `cn_number`, consignee details, the attempt
    `status` and `reason`, and any advice already given. Pass the row's `id`
    and `cn_number` to `update_shipper_advice`.
    """
    return await _call(
        "POST",
        "shipperAdviceList",
        _advice_filters(
            from_date, to_date, cn_number, status, product,
            origin_city, destination_city, start, length,
        ),
    )


@mcp.tool
async def get_activity_log(
    cn_number: Annotated[
        str | None, Field(description="Limit to one CN number.")
    ] = None,
    status: Annotated[
        str | None, Field(description="Optional status filter, as Leopards names it.")
    ] = None,
    product: Annotated[
        str | None, Field(description="Optional product filter, as Leopards names it.")
    ] = None,
    start: Annotated[int, Field(description="Row offset for paging.", ge=0)] = 0,
    length: Annotated[
        int, Field(description="Rows per page.", ge=1, le=500)
    ] = 100,
) -> dict:
    """Read the shipper-advice activity log: attempts, reasons and outcomes."""
    return await _call(
        "POST",
        "activityLog",
        {
            "product": product or "",
            "status": status or "",
            "Cn_number": cn_number or "",
            "start": start,
            "length": length,
        },
    )


@mcp.tool
async def update_shipper_advice(
    advice_id: Annotated[
        int, Field(description="The row `id` from `list_shipper_advice`.")
    ],
    cn_number: Annotated[str, Field(description="The packet's CN number.")],
    advice_status: Annotated[
        str,
        Field(
            description=(
                "'RA' or 'RT' -- the only two codes Leopards accepts. The "
                "guide lists them without defining them; they are understood "
                "to mean RA = re-attempt delivery and RT = return to shipper. "
                "Confirm the meaning with Leopards before first use."
            )
        ),
    ],
    remarks: Annotated[
        str, Field(description="Instruction for Leopards, e.g. a new phone number or time.")
    ],
) -> dict:
    """Tell Leopards what to do with a packet that could not be delivered.

    THIS CHANGES A REAL SHIPMENT (a return costs return charges). Confirm the
    CN number and the chosen advice with the user before calling.
    """
    code = advice_status.strip().upper()
    if code not in {"RA", "RT"}:
        raise ValueError("advice_status must be 'RA' or 'RT'.")
    return await _call(
        "POST",
        "updateShipperAdvice",
        {
            "data": [
                {
                    "id": advice_id,
                    "cn_number": cn_number.strip(),
                    "shipper_advice_status": code,
                    "shipper_remarks": remarks,
                }
            ]
        },
    )


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------
@mcp.tool
async def get_shipper_details(
    request_param: Annotated[
        str,
        Field(
            description=(
                "Which field to look the shipper up by. The guide does not "
                "list the allowed names; 'shipment_id' is the likely one."
            )
        ),
    ],
    request_value: Annotated[str, Field(description="The value to look up.")],
) -> dict:
    """Look up a shipper (pickup) profile on the account.

    Returns the shipper's ID, name, contact, address, city and settlement
    bank details.
    """
    return await _call(
        "GET",
        "getShipperDetails",
        {"request_param": request_param, "request_value": request_value},
    )


# --------------------------------------------------------------------------
# HTTP app
# --------------------------------------------------------------------------
# Stateless + JSON responses: each Vercel invocation is a fresh process, so
# there is nowhere to keep a session between requests.
mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

app = FastAPI(
    title="Leopards Courier MCP Server",
    description="Leopards Courier merchant API exposed over the Model Context Protocol.",
    version="1.0.0",
    lifespan=mcp_app.lifespan,
)


@app.get("/")
async def root() -> dict:
    """Human-readable landing page. Shows no secrets."""
    return {
        "name": "Leopards Courier MCP Server",
        "mcp_endpoint": "/mcp",
        "default_environment": DEFAULT_ENV,
        "server_stored_credentials": bool(ENV_API_KEY and ENV_API_PASSWORD),
        "access_key_configured": bool(ACCESS_KEY),
        "tools": sorted(t.name for t in await mcp.list_tools()),
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Mounted last so the routes above still resolve.
app.mount("/", mcp_app)
