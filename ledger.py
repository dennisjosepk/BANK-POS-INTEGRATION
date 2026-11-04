"""Local receipt storage. Amounts are stored as integers, never floating point."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from pos import BANK_METHOD, PosResult, new_request_id, parse_payment_result, safe_token


# Code: (display name, number of digits after the decimal point).
CURRENCIES = {
    "AED": ("UAE dirham", 2),
    "BHD": ("Bahraini dinar", 3),
    "EUR": ("Euro", 2),
    "GBP": ("British pound", 2),
    "INR": ("Indian rupee", 2),
    "JOD": ("Jordanian dinar", 3),
    "JPY": ("Japanese yen", 0),
    "KWD": ("Kuwaiti dinar", 3),
    "LKR": ("Sri Lankan rupee", 2),
    "NPR": ("Nepalese rupee", 2),
    "OMR": ("Omani rial", 3),
    "PHP": ("Philippine peso", 2),
    "PKR": ("Pakistani rupee", 2),
    "QAR": ("Qatari riyal", 2),
    "SAR": ("Saudi riyal", 2),
    "USD": ("US dollar", 2),
    "ZAR": ("South African rand", 2),
}
METHODS = ("Cash", "Card (manual entry)", BANK_METHOD, "Bank transfer", "Other")
MAX_MINOR = 999_999_999_999


def parse_amount(text: str, currency: str) -> int:
    digits = CURRENCIES[currency][1]
    text = text.strip()
    pattern = rf"[0-9]+(?:\.[0-9]{{1,{digits}}})?" if digits else r"[0-9]+"
    if len(text) > 30 or not re.fullmatch(pattern, text):
        raise ValueError(
            f"Enter an amount with up to {digits} decimal places, without commas or symbols."
        )
    amount = int(Decimal(text) * (10 ** digits))
    if not 0 < amount <= MAX_MINOR:
        raise ValueError("The amount must be greater than zero and within the supported range.")
    return amount


def format_amount(amount_minor: int, currency: str, grouping: bool = True) -> str:
    digits = CURRENCIES[currency][1]
    amount = Decimal(amount_minor) / (10 ** digits)
    return format(amount, f",.{digits}f" if grouping else f".{digits}f")


def spreadsheet_text(value: str) -> str:
    """Keep user-entered text from becoming a formula when opened in Excel."""
    trimmed = value.lstrip()
    if trimmed.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_date TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                    payer TEXT NOT NULL,
                    payer_phone TEXT NOT NULL DEFAULT '',
                    payer_address TEXT NOT NULL DEFAULT '',
                    method TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    voided_at TEXT,
                    card_brand TEXT NOT NULL DEFAULT '',
                    card_last4 TEXT NOT NULL DEFAULT '',
                    pos_provider_id TEXT NOT NULL DEFAULT '',
                    pos_transaction_id TEXT NOT NULL DEFAULT '',
                    pos_authorization_code TEXT NOT NULL DEFAULT '',
                    pos_terminal_id TEXT NOT NULL DEFAULT '',
                    pos_attempt_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS pos_attempts (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, provider_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL, payload TEXT NOT NULL, receipt_id INTEGER,
                    result_json TEXT NOT NULL DEFAULT ''
                );
            """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(receipts)")}
        missing = {"payer_phone", "payer_address"} - columns
        if missing:
            # Back up the original schema and records before upgrading an existing receipt book.
            backup = self.path.with_name(self.path.stem + "-before-printing-update.sqlite3")
            if not backup.exists():
                self.backup(backup)
            with self.connection:
                for name in sorted(missing):
                    self.connection.execute(f"ALTER TABLE receipts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        card_columns = {"card_brand", "card_last4", "pos_provider_id", "pos_transaction_id",
                        "pos_authorization_code", "pos_terminal_id", "pos_attempt_id"}
        missing = card_columns - columns
        if missing:
            backup = self.path.with_name(self.path.stem + "-before-bank-pos-update.sqlite3")
            if not backup.exists():
                self.backup(backup)
            with self.connection:
                for name in sorted(missing):
                    self.connection.execute(f"ALTER TABLE receipts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        with self.connection:
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS receipt_pos_attempt "
                                    "ON receipts(pos_attempt_id) WHERE pos_attempt_id <> ''")
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS receipt_pos_transaction "
                                    "ON receipts(pos_provider_id, pos_transaction_id) WHERE pos_transaction_id <> ''")

    def preference(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_preference(self, key: str, value: str) -> None:
        if key not in ("receipt_design", "print_after_save", "pos_config"):
            raise ValueError("Unknown application preference.")
        with self.connection:
            self.connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value)
            )

    @property
    def currency(self) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = 'currency'"
        ).fetchone()
        if row is None:
            return None
        if row[0] not in CURRENCIES:
            raise ValueError("This ledger uses an unsupported currency.")
        return row[0]

    def set_currency(self, code: str) -> None:
        if code not in CURRENCIES:
            raise ValueError("Choose a currency from the list.")
        with self.connection:
            if self.currency and self.currency != code:
                raise ValueError("The ledger currency cannot be changed. Use a separate ledger.")
            self.connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('currency', ?)", (code,)
            )

    def validate_entry(self, received_date: str, amount: str, payer: str = "", method: str = "Cash",
                       reference: str = "", notes: str = "", payer_phone: str = "", payer_address: str = "") -> dict:
        currency = self.currency
        if currency is None:
            raise ValueError("Choose the ledger currency first.")
        try:
            parsed_date = date.fromisoformat(received_date)
        except ValueError:
            raise ValueError("Enter the date as YYYY-MM-DD, for example 2026-11-03.") from None
        if parsed_date.isoformat() != received_date:
            raise ValueError("Enter the date as YYYY-MM-DD.")
        if parsed_date > date.today():
            raise ValueError("The received date cannot be in the future.")
        if method not in METHODS:
            raise ValueError("Choose a payment method from the list.")
        for name, value, limit in (("Payer", payer, 120), ("Reference", reference, 120),
                                   ("Notes", notes, 1000), ("Phone", payer_phone, 60),
                                   ("Address", payer_address, 500)):
            if len(value) > limit:
                raise ValueError(f"{name} must be {limit} characters or fewer.")
        minor = parse_amount(amount, currency)
        return dict(received_date=received_date, amount_minor=minor, payer=payer.strip(), method=method,
                    reference=reference.strip(), notes=notes.strip(), payer_phone=payer_phone.strip(),
                    payer_address=payer_address.strip())

    def _insert_receipt(self, values, *, result=None):
        card = result.safe_dict() if result else {}
        cursor = self.connection.execute(
            """INSERT INTO receipts
               (received_date, amount_minor, payer, method, reference, notes, created_at, payer_phone, payer_address,
                card_brand, card_last4, pos_provider_id, pos_transaction_id, pos_authorization_code, pos_terminal_id, pos_attempt_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (values["received_date"], values["amount_minor"], values["payer"], values["method"], values["reference"],
             values["notes"], datetime.now(timezone.utc).isoformat(timespec="seconds"), values["payer_phone"], values["payer_address"],
             card.get("card_brand", ""), card.get("card_last4", ""), card.get("provider_id", ""),
             card.get("transaction_id", ""), card.get("authorization_code", ""), card.get("terminal_id", ""), card.get("request_id", "")),
        )
        return cursor.lastrowid

    def add(self, received_date: str, amount: str, payer: str = "", method: str = "Cash",
            reference: str = "", notes: str = "", payer_phone: str = "", payer_address: str = "") -> int:
        values = self.validate_entry(received_date, amount, payer, method, reference, notes, payer_phone, payer_address)
        if method == BANK_METHOD:
            raise ValueError("Bank POS receipts can only be saved from a confirmed terminal approval.")
        with self.connection:
            return self._insert_receipt(values)

    def pending_pos(self):
        return self.connection.execute(
            "SELECT * FROM pos_attempts WHERE status IN ('pending', 'unknown') ORDER BY created_at"
        ).fetchall()

    def pos_attempt(self, request_id):
        row = self.connection.execute("SELECT * FROM pos_attempts WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Payment request not found.")
        return row

    def begin_pos_payment(self, entry: dict, provider_id: str):
        values = self.validate_entry(**entry)
        if values["method"] != BANK_METHOD:
            raise ValueError("Choose Card (Bank POS) for a terminal payment.")
        if self.currency != "SAR":
            raise ValueError("This bank POS workflow currently supports SAR payments only.")
        safe_token(provider_id, limit=80)
        request_id = new_request_id()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.pending_pos():
                raise ValueError("An earlier card payment is unconfirmed. Use Bank POS setup → Pending payments before taking another payment.")
            self.connection.execute(
                "INSERT INTO pos_attempts(id, created_at, updated_at, status, provider_id, amount_minor, currency, payload) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                (request_id, now, now, provider_id, values["amount_minor"], self.currency, json.dumps(values, ensure_ascii=False)),
            )
        return self.pos_attempt(request_id)

    def finish_pos_payment(self, result: PosResult):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            attempt = self.pos_attempt(result.request_id)
            if result.provider_id != attempt["provider_id"]:
                raise ValueError("The payment response came from a different provider.")
            safe = parse_payment_result(result.safe_dict(), attempt["id"], attempt["amount_minor"],
                                        attempt["currency"], attempt["provider_id"])
            if attempt["receipt_id"] is not None:
                receipt = self.receipt(attempt["receipt_id"])
                if safe.status != "approved" or receipt["pos_transaction_id"] != safe.transaction_id:
                    self.connection.execute("UPDATE pos_attempts SET status = 'unknown' WHERE id = ?", (attempt["id"],))
                    # Persist the reconciliation barrier even though this call reports a conflict.
                    self.connection.commit()
                    raise ValueError("This payment has already been recorded with a different result.")
                self.connection.execute("UPDATE pos_attempts SET status = 'recorded' WHERE id = ?", (attempt["id"],))
                return receipt["id"]
            if attempt["status"] in ("cancelled", "declined"):
                if safe.status != attempt["status"]:
                    self.connection.execute("UPDATE pos_attempts SET status = 'unknown' WHERE id = ?", (attempt["id"],))
                    self.connection.commit()
                    raise ValueError("The terminal changed a finalized payment outcome. Check the provider's records.")
                return None
            receipt_id = None
            status = safe.status
            if status == "approved":
                receipt_id = self._insert_receipt(json.loads(attempt["payload"]), result=safe)
                status = "recorded"
            self.connection.execute(
                "UPDATE pos_attempts SET status = ?, receipt_id = ?, updated_at = ?, result_json = ? WHERE id = ?",
                (status, receipt_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 json.dumps(safe.safe_dict()), attempt["id"]),
            )
            return receipt_id

    def receipts(self, date_from: str = "", date_to: str = "") -> list[sqlite3.Row]:
        for value in (date_from, date_to):
            if value:
                try:
                    parsed = date.fromisoformat(value)
                    if parsed.isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError("Filter dates must be YYYY-MM-DD.") from None
        if date_from and date_to and date_from > date_to:
            raise ValueError("From date must be on or before To date.")
        return self.connection.execute(
            "SELECT * FROM receipts WHERE (? = '' OR received_date >= ?) "
            "AND (? = '' OR received_date <= ?) ORDER BY received_date DESC, id DESC",
            (date_from, date_from, date_to, date_to),
        ).fetchall()

    def receipt(self, receipt_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Receipt not found.")
        return row

    def totals(self) -> tuple[int, int, int]:
        rows = self.connection.execute(
            "SELECT received_date, amount_minor FROM receipts WHERE voided_at IS NULL"
        ).fetchall()
        today = date.today().isoformat()
        return (sum(row[1] for row in rows if row[0] == today),
                sum(row[1] for row in rows), len(rows))

    def void(self, receipt_id: int) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE receipts SET voided_at = ? WHERE id = ? AND voided_at IS NULL",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), receipt_id),
            )
            if not cursor.rowcount:
                raise ValueError("This receipt was already voided or no longer exists.")

    def export_csv(self, path: Path, date_from: str = "", date_to: str = "") -> int:
        path = Path(path)
        if path.resolve() == self.path.resolve() or (path.exists() and path.samefile(self.path)):
            raise ValueError("Choose an export location different from the live database.")
        rows = self.receipts(date_from, date_to)
        currency = self.currency
        if currency is None:
            raise ValueError("Choose the ledger currency first.")
        with path.open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(("Receipt ID", "Received date", "Amount", "Currency", "Payer",
                             "Payment method", "Reference", "Notes", "Status",
                             "Recorded at (UTC)", "Voided at (UTC)", "Payer phone", "Payer address",
                             "Card brand", "Card last 4", "Bank transaction ID", "Authorization code", "Terminal ID", "POS provider"))
            for row in rows:
                writer.writerow((row["id"], row["received_date"],
                                 format_amount(row["amount_minor"], currency, grouping=False),
                                 currency, spreadsheet_text(row["payer"]), row["method"],
                                 spreadsheet_text(row["reference"]), spreadsheet_text(row["notes"]),
                                 "Voided" if row["voided_at"] else "Recorded",
                                 row["created_at"], row["voided_at"] or "",
                                 spreadsheet_text(row["payer_phone"]), spreadsheet_text(row["payer_address"]),
                                 row["card_brand"], "**** " + row["card_last4"] if row["card_last4"] else "", row["pos_transaction_id"],
                                 row["pos_authorization_code"], row["pos_terminal_id"], row["pos_provider_id"] ))
        return len(rows)

    def backup(self, path: Path) -> None:
        path = Path(path)
        if path.resolve() == self.path.resolve() or (path.exists() and path.samefile(self.path)):
            raise ValueError("Choose a backup location different from the live database.")
        with closing(sqlite3.connect(path)) as target:
            self.connection.backup(target)

    def close(self) -> None:
        self.connection.close()
