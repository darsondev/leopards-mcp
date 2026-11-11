# Leopards Courier MCP Server

Connects Claude to a Leopards Courier (LCS) merchant account, so Claude can
book, track, cancel and reconcile cash-on-delivery shipments.

Built against the Leopards **API Documentation v2.0**. Deploys to Vercel as a
single Python file (`app.py`).

## Tools

| Tool | Leopards endpoint | What it does |
| --- | --- | --- |
| `get_cities` | `getAllCities` | Find city IDs, with origin/destination flags |
| `book_packet` | `bookPacket` | Book one packet; returns the CN and slip link |
| `cancel_packets` | `cancelBookedPackets` | Cancel booked packets |
| `track_packets` | `trackBookedPacket` | Status and full journey for one or more CNs |
| `list_packets_by_date` | `getBookedPacketLastStatus` | Packets booked in a date range, with latest status |
| `get_shipments_by_order_id` | `getShipmentDetailsByOrderID` | Look up shipments by your own order ID |
| `get_proof_of_delivery` | `getElectronicProofOfDelivery` | Receiver, location and signature link |
| `generate_load_sheet` | `generateLoadSheet` | Create a pickup load sheet |
| `download_load_sheet` | `downloadLoadSheet` | Load sheet as data or as a PDF |
| `get_payment_details` | `getPaymentDetails` | COD payment status per CN |
| `get_shipping_charges` | `getShippingCharges` | What Leopards charged per CN |
| `get_tariff` | `getTariffDetails` | Estimate charges before booking |
| `list_invoices` | `getInvoices` | COD invoices / payment cheques |
| `list_shipper_advice` | `shipperAdviceList` | Failed-delivery packets awaiting advice |
| `get_activity_log` | `activityLog` | Shipper-advice activity log |
| `update_shipper_advice` | `updateShipperAdvice` | Give advice (RA / RT) on a packet |
| `get_shipper_details` | `getShipperDetails` | Shipper (pickup) profile on the account |

Not included: batch booking (the guide's addresses for it do not exist on the
live server; book packets one at a time instead), bank list and create shipper.

## Deploy to Vercel

1. Create a new GitHub repository and upload `app.py`, `requirements.txt`,
   `vercel.json` and `README.md` to it.
2. In Vercel, add a new project and import that repository.
3. Before clicking Deploy, open **Environment Variables** and add:

   | Name | Value |
   | --- | --- |
   | `LCS_API_KEY` | your Leopards API key |
   | `LCS_API_PASSWORD` | your Leopards API password |
   | `MCP_ACCESS_KEY` | a long random secret you invent (letters and numbers only) |

4. Click Deploy. Open `https://<project>.vercel.app/health` to confirm it runs.
5. Open `https://<project>.vercel.app/` and check that both
   `server_stored_credentials` and `access_key_configured` are `true`.

The Leopards key and password come from the Leopards merchant portal, under
**API Settings > API Management**.

## Connect to Claude

Add a custom connector with this address:

```
https://<project>.vercel.app/mcp?key=<MCP_ACCESS_KEY>
```

Treat that address like a password: anyone who has it can book and cancel
shipments on the account. To lock it again, change `MCP_ACCESS_KEY` in Vercel
and redeploy; the Leopards credentials stay as they are.

### Alternative: no stored credentials

Leave the environment variables unset and put the credentials in the address:

```
https://<project>.vercel.app/mcp?api_key=<key>&api_password=<password>
```

If the password contains symbols such as `#`, `&`, `+` or `%`, they must be
URL-encoded, which is why the stored-credentials route is recommended.

## Test (staging) mode

Add `&env=staging` to the connector address to use Leopards' staging server.
Staging needs its own staging credentials from Leopards. To make staging the
default, set the environment variable `LCS_ENV=staging`.

## Safety

- `book_packet`, `cancel_packets`, `generate_load_sheet` and
  `update_shipper_advice` change real shipments and real money. Their
  descriptions tell Claude to confirm first, but that is a prompt-level
  guardrail, not an enforced one. Review what you approve.
- Stored credentials are used only when the request carries the matching
  `MCP_ACCESS_KEY`. If the credentials are set but the access key is not, the
  server refuses to use them.
- Credentials are sent to Leopards only. They are not logged by this code and
  never appear in error messages. Vercel's own request logs do record the
  address used, including the `?key=` part.

## What has and has not been tested

- Every endpoint name and its GET/POST method was checked against the live
  Leopards server with dummy credentials.
- Request building, credential handling and error handling were tested against
  a stand-in server.
- Nothing has been run with a real Leopards account, so response details and
  optional fields may need small fixes on first use. Known uncertain spots:
  - `list_shipper_advice`: the guide documents this endpoint twice with
    different field names; both sets are sent.
  - `update_shipper_advice`: the guide lists codes `RA` and `RT` without
    defining them.
  - `get_tariff`: the guide does not list shipment type IDs.
  - `get_shipper_details`: the guide does not list the allowed `request_param`
    names.
