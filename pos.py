"""Bank POS connector boundary. No guessed serial commands or simulated approvals.

A provider-specific helper must implement the documented stdio protocol using
its supported bank/terminal SDK. Tests inject their own helpers; the shipped app
contains no mock-payment provider.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

PROTOCOL = "receipt-recorder-pos/1"
BANK_METHOD = "Card (Bank POS)"
BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
FINAL_STATUSES = {"approved", "declined", "cancelled", "unknown"}
PROGRESS = {
    "connecting": "Connecting to the bank POS terminal…",
    "waiting_card": "Present the card on the bank POS terminal.",
    "processing": "The terminal is processing the payment…",
    "cancelling": "Cancellation requested. Waiting for the terminal to confirm…",
}


class PosError(ValueError):
    pass


def serial_ports() -> list[str]:
    """Enumerate USB/serial ports without opening a terminal or sending data."""
    if sys.platform != "win32":
        return []
    import winreg
    result = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                if re.fullmatch(r"COM[1-9][0-9]{0,3}", str(value), re.I):
                    result.add(str(value).upper())
                index += 1
    except OSError:
        pass
    return sorted(result, key=lambda value: int(value[3:]))


def safe_token(value, *, limit=160, optional=False) -> str:
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0," + str(limit - 1) + "}", value):
        raise PosError("The connector returned an invalid transaction identifier.")
    return value


@dataclass
class PosConfig:
    supplier: str = ""
    terminal_model: str = ""
    connector_path: str = ""
    port: str = ""
    baud_rate: int = 115200
    data_bits: int = 8
    parity: str = "None"
    stop_bits: str = "1"
    flow_control: str = "None"
    timeout_seconds: int = 120
    terminal_id: str = ""
    provider_id: str = ""
    verified_fingerprint: str = ""

    def validate(self, *, require_verified=True):
        for value in (self.supplier, self.terminal_model):
            if not isinstance(value, str) or len(value) > 120:
                raise PosError("Supplier and terminal model must be 120 characters or fewer.")
        if not re.fullmatch(r"COM[1-9][0-9]{0,3}", self.port):
            raise PosError("Choose the terminal's Windows COM port, for example COM3.")
        if type(self.baud_rate) is not int or self.baud_rate not in BAUD_RATES:
            raise PosError("Choose a baud rate supported by your supplier's ECR settings.")
        if type(self.data_bits) is not int or self.data_bits not in (5, 6, 7, 8):
            raise PosError("Choose valid serial data bits.")
        if self.parity not in ("None", "Even", "Odd", "Mark", "Space") or self.stop_bits not in ("1", "1.5", "2"):
            raise PosError("Choose valid parity and stop bits.")
        if self.flow_control not in ("None", "RTS/CTS", "XON/XOFF"):
            raise PosError("Choose a supported serial flow control.")
        if type(self.timeout_seconds) is not int or not 15 <= self.timeout_seconds <= 300:
            raise PosError("Payment timeout must be between 15 and 300 seconds.")
        safe_token(self.terminal_id, limit=80, optional=True)
        path = Path(self.connector_path)
        if not self.connector_path or path.suffix.lower() != ".exe" or not path.is_file():
            raise PosError("A compatible bank POS connector is required. A COM port alone cannot process payments.")
        if require_verified and (not self.provider_id or self.verified_fingerprint != self.fingerprint()):
            raise PosError("Open Bank POS setup and check the terminal connection before taking a payment.")
        if self.provider_id:
            safe_token(self.provider_id, limit=80)

    def fingerprint(self):
        digest = hashlib.sha256()
        try:
            with Path(self.connector_path).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            raise PosError("The configured connector is unavailable. Open Bank POS setup.") from None
        settings = dict(path=str(Path(self.connector_path).resolve()), connection=self.connection(), file_hash=digest.hexdigest())
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()

    def connection(self):
        return dict(transport="usb_serial", port=self.port, baud_rate=self.baud_rate, terminal_id=self.terminal_id,
                    data_bits=self.data_bits, parity=self.parity, stop_bits=self.stop_bits, flow_control=self.flow_control)

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        if not text:
            return cls()
        try:
            values = json.loads(text)
            if not isinstance(values, dict):
                raise ValueError
            return cls(**values)
        except (ValueError, TypeError):
            raise PosError("The POS settings are invalid. Open Bank POS setup to configure them again.") from None


@dataclass(frozen=True)
class PosResult:
    request_id: str
    status: str
    provider_id: str
    amount_minor: int = 0
    currency: str = "SAR"
    transaction_id: str = ""
    card_brand: str = ""
    card_last4: str = ""
    authorization_code: str = ""
    terminal_id: str = ""

    def safe_dict(self):
        return asdict(self)


def parse_payment_result(packet, request_id, amount_minor, currency, provider_id):
    """Validate correlation and retain only the permitted, non-sensitive fields."""
    if packet.get("request_id") != request_id or packet.get("status") not in FINAL_STATUSES:
        raise PosError("The terminal response could not be matched to this payment. Check its status before retrying.")
    status = packet["status"]
    if status != "approved":
        return PosResult(request_id, status, provider_id, amount_minor, currency)
    if type(packet.get("amount_minor")) is not int or packet["amount_minor"] != amount_minor or packet.get("currency") != currency:
        raise PosError("The approved amount or currency did not match this payment. Check the terminal before retrying.")
    transaction_id = safe_token(packet.get("transaction_id"))
    brand = packet.get("card_brand", "")
    if not isinstance(brand, str) or not re.fullmatch(r"[A-Za-z][A-Za-z .-]{0,39}", brand):
        brand = ""
    aliases = {"visa": "Visa", "mastercard": "Mastercard", "master card": "Mastercard", "mada": "mada",
               "amex": "American Express", "american express": "American Express"}
    brand = aliases.get(brand.strip().lower(), brand.strip())
    last4 = packet.get("card_last4", "")
    if not isinstance(last4, str) or not re.fullmatch(r"[0-9]{4}", last4):
        last4 = ""  # Never retain a full PAN, track, PIN or CVV from a malformed helper response.
    def optional_identifier(key, limit):
        try:
            return safe_token(packet.get(key), limit=limit, optional=True)
        except PosError:
            return ""  # Missing optional metadata must not discard a confirmed financial approval.
    return PosResult(request_id, status, provider_id, amount_minor, currency, transaction_id, brand, last4,
                     optional_identifier("authorization_code", 64), optional_identifier("terminal_id", 80))


class BankConnector:
    """One helper process per operation; cancellation must be terminal-confirmed."""

    def __init__(self, config: PosConfig):
        self.config = config

    def run(self, operation, request_id, amount_minor, currency, cancel, progress):
        self.config.validate(require_verified=operation != "check_connection")
        if operation not in ("check_connection", "purchase", "status"):
            raise PosError("Unsupported POS operation.")
        process = None
        sent = False
        cancellation_sent = False
        messages = queue.Queue(maxsize=100)
        reader_stop = threading.Event()
        provider_id = self.config.provider_id
        pre_send_status = "unknown" if operation == "status" else "cancelled"
        try:
            progress("connecting")
            if cancel.is_set():
                return PosResult(request_id, pre_send_status, provider_id, amount_minor, currency)
            process = subprocess.Popen(
                [self.config.connector_path, "--receipt-recorder-stdio"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            def read_messages():
                def deliver(value):
                    while not reader_stop.is_set():
                        try:
                            messages.put(value, timeout=0.1)
                            return
                        except queue.Full:
                            continue
                try:
                    while not reader_stop.is_set():
                        line = process.stdout.readline(16_385)
                        if not line or len(line) > 16_384:
                            deliver(None)
                            return
                        try:
                            value = json.loads(line)
                            if not isinstance(value, dict):
                                raise ValueError
                        except (ValueError, UnicodeError):
                            deliver(None)
                            return
                        deliver(value)
                except (OSError, ValueError):
                    deliver(None)
                finally:
                    try:
                        process.stdout.close()
                    except (OSError, ValueError):
                        pass
            threading.Thread(target=read_messages, daemon=True).start()

            def send(value):
                process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
                process.stdin.flush()

            send(dict(op="hello", protocol=PROTOCOL))
            deadline = time.monotonic() + 15
            while True:
                if cancel.is_set():
                    return PosResult(request_id, pre_send_status, provider_id, amount_minor, currency)
                try:
                    hello = messages.get(timeout=0.1)
                    break
                except queue.Empty:
                    if time.monotonic() > deadline:
                        raise PosError("The selected program did not answer as a compatible POS connector.")
            required = {"purchase", "cancel", "status", "check_connection"}
            if (not hello or hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL
                    or not isinstance(hello.get("capabilities"), list) or not required.issubset(hello["capabilities"])):
                raise PosError("The selected program is not a supported POS connector with cancellation and status checking.")
            provider_id = safe_token(hello.get("provider_id"), limit=80)
            if self.config.provider_id and provider_id != self.config.provider_id:
                raise PosError("The connector provider changed. Check its connection in POS setup again.")
            if cancel.is_set():
                return PosResult(request_id, pre_send_status, provider_id, amount_minor, currency)
            request = dict(op=operation, request_id=request_id, connection=self.config.connection())
            if operation != "check_connection":
                request.update(amount_minor=amount_minor, currency=currency)
            # From this point on any failure is an unknown outcome, not a failed/cancelled payment.
            sent = True
            send(request)
            deadline = time.monotonic() + self.config.timeout_seconds
            cancel_deadline = None
            while True:
                if cancel.is_set() and operation != "purchase":
                    return PosResult(request_id, pre_send_status, provider_id, amount_minor, currency)
                if operation == "purchase" and not cancellation_sent and (cancel.is_set() or time.monotonic() > deadline):
                    progress("cancelling")
                    send(dict(op="cancel", request_id=request_id))
                    cancellation_sent = True
                    cancel_deadline = time.monotonic() + 15
                if (cancel_deadline and time.monotonic() > cancel_deadline) or (
                        operation != "purchase" and time.monotonic() > deadline):
                    return PosResult(request_id, "unknown", provider_id, amount_minor, currency)
                try:
                    packet = messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if packet is None:
                    return PosResult(request_id, "unknown", provider_id, amount_minor, currency)
                if packet.get("request_id") != request_id:
                    raise PosError("The terminal returned a response for a different payment.")
                if packet.get("type") == "progress":
                    stage = packet.get("stage")
                    if stage in PROGRESS and not cancellation_sent:
                        progress(stage)
                    continue
                if packet.get("type") != "result":
                    raise PosError("The connector returned an unsupported response.")
                if operation == "check_connection":
                    if packet.get("status") != "connected":
                        raise PosError("The connector could not confirm a terminal connection. Check ECR mode, driver and COM settings.")
                    return PosResult(request_id, "connected", provider_id)
                if (packet.get("status") == "approved" and self.config.terminal_id
                        and packet.get("terminal_id") != self.config.terminal_id):
                    raise PosError("The approval came from a different terminal.")
                return parse_payment_result(packet, request_id, amount_minor, currency, provider_id)
        except (OSError, ValueError, TypeError, KeyError):
            if sent and operation != "check_connection":
                return PosResult(request_id, "unknown", provider_id, amount_minor, currency)
            raise PosError("Could not confirm a connection. Verify the bank connector, ECR mode and USB/COM settings.") from None
        finally:
            reader_stop.set()
            if process is not None:
                try:
                    # EOF lets a helper flush its durable result and disconnect cleanly.
                    # Stopping the helper is never treated as a confirmed bank cancellation.
                    process.stdin.close()
                    if process.poll() is None:
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass


def new_request_id():
    return str(uuid4())
