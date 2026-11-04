"""Small, shareable diagnostic logs with an explicit allowlist of safe fields.

Never pass raw SDK packets, exception text, receipts or environment values here.
Exports are parsed and allowlisted again instead of blindly copying log files.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
from uuid import uuid4

EVENTS = {
    "app_start", "app_close", "startup_error", "ui_error", "receipt_save_error", "print_error",
    "csv_error", "backup_error", "receipt_void_error", "pos_start_error", "pos_record_error", "pos_recorded",
    "pos_result", "pos_status_error", "pos_config_error", "pos_config_saved", "pos_check_requested",
    "pos_check_result", "pos_operation_start", "pos_validation_error", "pos_helper_start",
    "pos_handshake_ok", "pos_handshake_timeout", "pos_invalid_frame", "pos_operation_sent",
    "pos_progress", "pos_cancel_requested", "pos_timeout", "pos_helper_exit", "pos_operation_result",
    "pos_operation_error", "pos_ui_cancel", "ports_detected", "usb_probe_start", "usb_probe_result",
    "usb_probe_error", "diagnostic_export", "diagnostic_export_error",
}
ERROR_CODES = {
    "pos_error", "invalid_config", "invalid_port", "invalid_baud", "invalid_serial_parameters", "invalid_timeout",
    "invalid_identifier", "connector_missing", "connector_unavailable", "connection_not_verified", "unsupported_operation",
    "handshake_timeout", "unsupported_connector", "provider_changed", "connection_unconfirmed", "response_mismatch",
    "amount_mismatch", "terminal_mismatch", "invalid_frame", "helper_exit", "cancel_unconfirmed", "operation_timeout",
    "operation_active", "payment_pending", "unexpected_error", "os_error", "database_error", "validation_error",
    "export_error", "log_destination_invalid",
}
ENUMS = {
    "operation": {"check_connection", "purchase", "status"},
    "stage": {"connecting", "waiting_card", "processing", "cancelling"},
    "status": {"approved", "cancelled", "declined", "unknown", "connected", "not_confirmed"},
    "parity": {"None", "Even", "Odd", "Mark", "Space"},
    "stop_bits": {"1", "1.5", "2"}, "flow_control": {"None", "RTS/CTS", "XON/XOFF"},
    "host_parity": {"None", "Even", "Odd", "Mark", "Space"},
    "host_stop_bits": {"1", "1.5", "2"}, "host_flow_control": {"None", "RTS/CTS", "XON/XOFF"},
    "result_code": {"port_invalid", "platform_unsupported", "port_missing", "port_opened", "port_busy",
                    "port_access_denied", "port_open_failed", "serial_query_failed", "skipped_pending", "skipped_active"},
    "reason": {"user", "timeout"},
    "usb_identity_source": {"registry_port_mapping"},
}
BOOL_FIELDS = {"submitted", "connector_configured", "connector_exists", "connector_is_exe", "settings_checked",
               "port_present", "serial_settings_read", "diagnostics_available", "frozen"}
INT_FIELDS = {"baud_rate": (1, 4_000_000), "data_bits": (5, 8), "timeout_seconds": (1, 3600),
              "host_baud_rate": (0, 4_000_000), "host_data_bits": (5, 8),
              "host_flags": (0, 0xFFFFFFFF), "sqlite_code": (0, 65535),
              "win_error": (0, 65535), "errno": (0, 65535), "port_count": (0, 1000)}
PRODUCT_FILES = {"app.py", "pos.py", "pos_ui.py", "ledger.py", "receipt_printing.py", "receipt_designer.py",
                 "diagnostics.py", "diagnostics_ui.py", "usb_diagnostics.py"}
EXCEPTION_TYPES = {"OSError", "PermissionError", "FileNotFoundError", "ConnectionError", "TimeoutError",
                   "BrokenPipeError", "ValueError", "TypeError", "KeyError", "RuntimeError", "TclError", "PosError",
                   "Error", "OperationalError", "IntegrityError", "DatabaseError", "ProgrammingError", "AssertionError"}


def attempt_tag(request_id):
    return hashlib.sha256(str(request_id).encode()).hexdigest()[:12]


def safe_fields(values):
    """Drop unknown fields and unexpected values; do not try to redact arbitrary text."""
    result = {}
    for key, value in values.items():
        if key in BOOL_FIELDS and type(value) is bool:
            result[key] = value
        elif key in INT_FIELDS and type(value) is int and INT_FIELDS[key][0] <= value <= INT_FIELDS[key][1]:
            result[key] = value
        elif key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            result[key] = value
        elif key == "error_code" and isinstance(value, str) and value in ERROR_CODES:
            result[key] = value
        elif key == "port" and isinstance(value, str) and re.fullmatch(r"COM[1-9][0-9]{0,3}", value):
            result[key] = value
        elif key == "ports" and isinstance(value, list):
            result[key] = [p for p in value[:1000] if isinstance(p, str) and re.fullmatch(r"COM[1-9][0-9]{0,3}", p)]
        elif key == "attempt_tag" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value):
            result[key] = value
        elif key in ("usb_vid", "usb_pid") and isinstance(value, str) and re.fullmatch(r"[0-9A-F]{4}", value):
            result[key] = value
        elif key == "exception_type" and isinstance(value, str):
            result[key] = value if value in EXCEPTION_TYPES else "OtherException"
        elif key == "frames" and isinstance(value, list):
            accepted = []
            for frame in value[-12:]:
                if not isinstance(frame, dict):
                    continue
                file, line = frame.get("file"), frame.get("line")
                if isinstance(file, str) and file in PRODUCT_FILES and type(line) is int and 0 < line < 100000:
                    accepted.append(dict(file=file, line=line))
            result[key] = accepted
    return result


def config_fields(config):
    values = config if isinstance(config, dict) else vars(config)
    settings = {}
    for key in ("port", "baud_rate", "data_bits", "parity", "stop_bits", "flow_control", "timeout_seconds"):
        value = values.get(key)
        if key == "port" and isinstance(value, str):
            value = value.strip().upper()
        if key in ("baud_rate", "data_bits", "timeout_seconds") and isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                continue
        settings[key] = value
    path = values.get("connector_path")
    settings["connector_configured"] = isinstance(path, str) and bool(path)
    try:
        if not (isinstance(path, str) and path.startswith(("\\\\", "//"))):
            settings["connector_exists"] = bool(path) and Path(path).is_file()
        settings["connector_is_exe"] = bool(path) and Path(path).suffix.lower() == ".exe"
    except (OSError, ValueError, TypeError):
        settings["connector_exists"] = False
        settings["connector_is_exe"] = False
    settings["settings_checked"] = bool(values.get("provider_id") and values.get("verified_fingerprint"))
    return safe_fields(settings)


class DiagnosticLog:
    def __init__(self, directory, version, *, max_bytes=1_000_000, backups=3):
        self.directory = Path(directory)
        self.path = self.directory / "receipt-recorder.log"
        self.version = version if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) else "unknown"
        self.session = uuid4().hex[:12]
        self.max_bytes, self.backups = max(256, max_bytes), max(1, min(backups, 5))
        self.lock = threading.RLock()
        self.available = True

    def record(self, event, **values):
        if not isinstance(event, str) or event not in EVENTS:
            return False
        record = dict(time_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      version=self.version, session=self.session, event=event, **safe_fields(values))
        encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        try:
            with self.lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        target = self.directory / f"receipt-recorder.log.{index}"
                        source = self.path if index == 1 else self.directory / f"receipt-recorder.log.{index - 1}"
                        if source.exists():
                            source.replace(target)
                with self.path.open("ab") as output:
                    output.write(encoded)
            self.available = True
            return True
        except OSError:
            self.available = False
            return False  # Diagnostic I/O must not change a payment or receipt outcome.

    def exception(self, event, error, **values):
        frames, tb = [], error.__traceback__
        while tb is not None:
            file = Path(tb.tb_frame.f_code.co_filename).name
            if file in PRODUCT_FILES:
                frames.append(dict(file=file, line=tb.tb_lineno))
            tb = tb.tb_next
        values.update(exception_type=type(error).__name__, frames=frames)
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in ERROR_CODES:
            values["error_code"] = code
        elif isinstance(error, OSError):
            values.setdefault("error_code", "os_error")
        elif type(error).__module__ == "sqlite3":
            values.setdefault("error_code", "database_error")
        elif isinstance(error, ValueError):
            values.setdefault("error_code", "validation_error")
        else:
            values.setdefault("error_code", "unexpected_error")
        for key in ("errno", "win_error"):
            value = getattr(error, "winerror" if key == "win_error" else key, None)
            if type(value) is int:
                values[key] = value
        number = getattr(error, "sqlite_errorcode", None)
        if type(number) is int:
            values["sqlite_code"] = number
        return self.record(event, **values)

    def _safe_records(self):
        files = [self.directory / f"receipt-recorder.log.{n}" for n in range(self.backups, 0, -1)] + [self.path]
        for file in files:
            if not file.exists():
                continue
            with file.open("rb") as source:
                while True:
                    line = source.readline(16385)
                    if not line:
                        break
                    if len(line) > 16384:
                        while line and not line.endswith(b"\n"):
                            line = source.readline(16385)
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or row.get("event") not in EVENTS:
                            continue
                        stamp = row.get("time_utc")
                        parsed = datetime.fromisoformat(stamp)
                        if parsed.tzinfo is None:
                            continue
                        version, session = row.get("version"), row.get("session")
                        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                            continue
                        if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{12}", session):
                            continue
                        yield dict(time_utc=parsed.astimezone(timezone.utc).isoformat(), version=version, session=session,
                                   event=row["event"], **safe_fields(row))
                    except (ValueError, TypeError, UnicodeError):
                        continue

    def recent(self, limit=40):
        try:
            with self.lock:
                return list(deque(self._safe_records(), maxlen=limit))
        except OSError:
            return []

    def export(self, destination, *, snapshot=None, protected=()):
        destination = Path(destination)
        # An export must never overwrite the database, generated logs or an application file.
        if (destination.suffix.lower() != ".txt" or destination.resolve().suffix.lower() != ".txt"
                or destination.resolve().is_relative_to(self.directory.resolve())
                or any(destination.resolve() == Path(path).resolve() or
                       (destination.exists() and Path(path).exists() and destination.samefile(path)) for path in protected)):
            raise ValueError("Choose a .txt file outside the application log folder and database.")
        os_name = platform.system()
        os_name = os_name if os_name in ("Windows", "Linux", "Darwin") else "Other"
        release = platform.release()
        match = re.match(r"[0-9]+(?:\.[0-9]+){0,3}", release)
        release = match.group() if match else "unknown"
        metadata = dict(application="Receipt Recorder", version=self.version, os=os_name, os_release=release,
                        python=".".join(map(str, sys.version_info[:3])), frozen=bool(getattr(sys, "frozen", False)),
                        exported_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        settings=safe_fields(snapshot or {}), diagnostics_available=self.available)
        with self.lock:
            with destination.open("w", encoding="utf-8", newline="\n") as output:
                output.write("Receipt Recorder diagnostic report — times are UTC\n")
                output.write("Includes app/OS versions, COM settings and coded errors. No receipt database or raw SDK responses.\n\n")
                output.write("Configured serial settings may be defaults. Host settings from a USB check do not identify the terminal protocol.\n\n")
                output.write(json.dumps(metadata, ensure_ascii=True, indent=2) + "\n\nEvents:\n")
                for row in self._safe_records():
                    output.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
        self.record("diagnostic_export")
        return destination


class NullDiagnosticLog:
    def record(self, *args, **kwargs): pass
    def exception(self, *args, **kwargs): pass
