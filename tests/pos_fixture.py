"""Test-only connector process. Never bundled or offered as a payment provider."""

import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


def main():
    scenario, audit = sys.argv[1:3]
    request = None
    for line in sys.stdin:
        message = json.loads(line)
        with open(audit, "a", encoding="utf-8") as stream:
            stream.write(message["op"] + "\n")
        if message["op"] == "hello":
            send(dict(type="hello", protocol="unsupported" if scenario == "bad_protocol" else "receipt-recorder-pos/1",
                      provider_id="fixture-provider", capabilities=["purchase", "cancel", "status", "check_connection"]))
            continue
        if message["op"] == "check_connection":
            send(dict(type="result", request_id=message["request_id"], status="connected"))
            continue
        if message["op"] == "cancel":
            if scenario == "cancel_ack_only":
                send(dict(type="progress", request_id=request["request_id"], stage="cancelling"))
                return
            if scenario == "timeout":
                continue
            status = "approved" if scenario == "late_approval" else "cancelled"
        else:
            request = message
            if scenario == "disconnect":
                return
            if scenario in ("wait_cancel", "late_approval", "cancel_ack_only", "timeout"):
                send(dict(type="progress", request_id=request["request_id"], stage="processing" if scenario == "timeout" else "waiting_card"))
                continue
            status = {"terminal_cancel": "cancelled", "declined": "declined"}.get(scenario, "approved")
        send(dict(type="result", request_id="wrong-request" if scenario == "wrong_id" else request["request_id"],
                  status=status, amount_minor=request.get("amount_minor", 0) + (1 if scenario == "wrong_amount" else 0),
                  currency=request.get("currency", "SAR"), transaction_id="TEST-" + request["request_id"],
                  card_brand="VISA", card_last4="1234", authorization_code="A1", terminal_id="TERM1"))


if __name__ == "__main__":
    main()
