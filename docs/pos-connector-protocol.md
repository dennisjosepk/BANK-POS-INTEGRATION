# Supplier POS connector interface, version 1

This document specifies the application's **local helper interface**, not a terminal's ECR wire protocol.
An adapter must translate it into the supplier's documented SDK/protocol. No live vendor adapter is supplied.
USB, COM-port discovery, baud rate and enabling ECR are prerequisites, not universal payment commands.
The same adapter can support multiple suppliers/models only where their supported protocol and responses match.

## Supplier information needed before implementation

Obtain the official ECR protocol/SDK, supported terminal models and firmware, Windows USB driver and serial
parameters, approval/decline/cancellation response definitions, card-brand and masked-card fields, timeout
semantics, transaction identifiers, and the status-query/reconciliation procedure. Use supplier test access
to verify purchase, app cancellation, terminal cancellation, late approval, disconnection and recovery.
Never infer approval from opening a COM port, writing bytes, or an SDK's command-send acknowledgement.

The supplied sample uses a .NET Mada SDK. Its GUI executable does not implement this interface, and a
reference to the mada card network does not prove a common ECR protocol for every Saudi terminal.

## Launch and framing

The app launches the selected `.exe` with one argument, `--receipt-recorder-stdio`, without a shell.
Use UTF-8 newline-delimited JSON on stdin/stdout, one JSON object per line, maximum 16 KiB per response.
Stdout is reserved for protocol frames; do not emit SDK logs or card data. The app discards helper stderr.
Configure any underlying SDK logging so it does not create files containing sensitive card data.
All example identifiers and card endings below are fictional.

Each process handles one check/purchase/status operation. Read stdin concurrently while an SDK transaction
is active so cancellation can be handled. Closing/killing a helper is **not** cancellation of a bank payment.

The app sends:

```json
{"op":"hello","protocol":"receipt-recorder-pos/1"}
```

The adapter must reply before any financial request:

```json
{"type":"hello","protocol":"receipt-recorder-pos/1","provider_id":"supplier.protocol.v1","capabilities":["purchase","cancel","status","check_connection"]}
```

`provider_id` is a stable, non-secret identity for the adapter's transaction namespace. It must match the
provider saved after a successful connection check. The app records a hash of the selected helper and
connection settings and requires another connection check after they change.

## Non-financial connection check

```json
{"op":"check_connection","request_id":"request-uuid","connection":{"transport":"usb_serial","port":"COM3","baud_rate":115200,"data_bits":8,"parity":"None","stop_bits":"1","flow_control":"None","terminal_id":""}}
```

The settings are examples, not defaults that work on every terminal. Confirm the terminal's actual ECR
handshake using its SDK's non-financial test/status command. Merely seeing/opening the port is insufficient.

```json
{"type":"result","request_id":"request-uuid","status":"connected"}
```

If a real handshake cannot be confirmed, return `status: "unknown"`; do not report connected.

## Purchase

```json
{"op":"purchase","request_id":"payment-uuid","amount_minor":10025,"currency":"SAR","connection":{"transport":"usb_serial","port":"COM3","baud_rate":115200,"data_bits":8,"parity":"None","stop_bits":"1","flow_control":"None","terminal_id":""}}
```

Amounts are integer halalas: `10025` means SAR 100.25. Only SAR is currently supported by this flow.
Use the unique `request_id` for correlation and idempotency. Persist enough request state in the adapter
to avoid replaying a purchase if it receives the same ID again; query the original transaction instead.
Where terminal recovery is impossible, retain an unknown outcome rather than sending another charge.
The application journals the entered receipt before submission and does not retry purchases automatically.

Progress frames have the same request ID. Supported stages are `connecting`, `waiting_card`, `processing`,
and `cancelling`. These change the loader text and cannot create a receipt.

```json
{"type":"progress","request_id":"payment-uuid","stage":"waiting_card"}
```

Only a documented, confirmed terminal approval may produce:

```json
{"type":"result","request_id":"payment-uuid","status":"approved","amount_minor":10025,"currency":"SAR","transaction_id":"TERM1.BATCH1.RRN123","card_brand":"mada","card_last4":"1234","authorization_code":"A123","terminal_id":"TERM1"}
```

- `transaction_id` must be stable and unique within `provider_id`, including any terminal/batch/date components
  necessary to disambiguate a terminal's counters. The application enforces its uniqueness and the request ID.
- `amount_minor`, `currency` and `request_id` must correspond to the actual approved transaction. No SDK response
  for a prior customer/amount may be relabelled with a new request ID.
- Forward only the four ending digits in `card_last4`, never a PAN or track. If an SDK returns a masked/full
  card value, reduce it inside the adapter; never log/forward the full value. Do not send a PIN or CVV.
- `card_brand` is the SDK's reported scheme (for example Visa, Mastercard or mada). Do not guess it from a PAN.
  Missing card details are allowed so an approved payment is not discarded, but the app will show them as unavailable.
- Identifier fields accept letters, digits and `_.:/-`, starting with a letter/digit. Brand accepts letters,
  spaces, dots and hyphens, up to 40 characters. No arbitrary/raw SDK payload is retained by the app.

Confirmed non-approved final outcomes are:

```json
{"type":"result","request_id":"payment-uuid","status":"declined"}
{"type":"result","request_id":"payment-uuid","status":"cancelled"}
{"type":"result","request_id":"payment-uuid","status":"unknown"}
```

A terminal-side user cancellation must use `cancelled` only after final terminal confirmation.

## App-side cancellation

During a purchase the app can send:

```json
{"op":"cancel","request_id":"payment-uuid"}
```

Ask the SDK to cancel the original transaction and continue listening for its final result. An accepted
cancel request is progress, not a cancelled result. If approval wins the race, return the approval so the
app records the actual received payment and explains that a refund is a separate provider operation.
After a timeout the app requests cancellation and allows another 15 seconds for confirmation. An unconfirmed
outcome stays pending even after the helper exits. Do not convert a timeout, lost response or helper shutdown
to a decline or cancellation.

## Query an existing payment

```json
{"op":"status","request_id":"original-payment-uuid","amount_minor":10025,"currency":"SAR","connection":{"transport":"usb_serial","port":"COM3","baud_rate":115200,"data_bits":8,"parity":"None","stop_bits":"1","flow_control":"None","terminal_id":""}}
```

Use the provider's status query/reconciliation mechanism and return the original confirmed result with
the same transaction ID. **Never resend a purchase to check its status.** A safe durable cache of a previously
confirmed result is acceptable; a missing cache entry is not evidence that a payment failed. Return unknown
until the provider can establish the result. If the SDK cannot confirm ambiguous transactions, that recovery
limitation must be resolved with the supplier before the adapter is used for live transactions.

The application stores unknown attempts across restarts and blocks new bank purchases until they resolve.
There is no button to invent an approval or silently remove an unknown payment. Manual receipt methods
remain separate; voiding a receipt does not refund a bank transaction.

## Testing

`tests/pos_fixture.py` is a test-only protocol peer, not a vendor implementation or a selectable demo provider.
The suite exercises real helper-process framing, cancellations and unknown outcomes using this fixture.
The shipped EXE contains no vendor SDK, test peer or simulated payment choice. Live supplier/terminal tests
are a separate prerequisite, even after all application and Windows build checks pass.
