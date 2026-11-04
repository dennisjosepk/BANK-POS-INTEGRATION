"""Local receipt storage. Amounts are stored as integers, never floating point."""

from __future__ import annotations

import csv
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path


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
METHODS = ("Cash", "Card (manual entry)", "Bank transfer", "Other")
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
                    voided_at TEXT
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

    def preference(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_preference(self, key: str, value: str) -> None:
        if key not in ("receipt_design", "print_after_save"):
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

    def add(self, received_date: str, amount: str, payer: str = "", method: str = "Cash",
            reference: str = "", notes: str = "", payer_phone: str = "", payer_address: str = "") -> int:
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
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO receipts
                   (received_date, amount_minor, payer, method, reference, notes, created_at, payer_phone, payer_address)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (received_date, minor, payer.strip(), method, reference.strip(), notes.strip(),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), payer_phone.strip(), payer_address.strip()),
            )
        return cursor.lastrowid

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
                             "Recorded at (UTC)", "Voided at (UTC)", "Payer phone", "Payer address"))
            for row in rows:
                writer.writerow((row["id"], row["received_date"],
                                 format_amount(row["amount_minor"], currency, grouping=False),
                                 currency, spreadsheet_text(row["payer"]), row["method"],
                                 spreadsheet_text(row["reference"]), spreadsheet_text(row["notes"]),
                                 "Voided" if row["voided_at"] else "Recorded",
                                 row["created_at"], row["voided_at"] or "",
                                 spreadsheet_text(row["payer_phone"]), spreadsheet_text(row["payer_address"])))
        return len(rows)

    def backup(self, path: Path) -> None:
        path = Path(path)
        if path.resolve() == self.path.resolve() or (path.exists() and path.samefile(self.path)):
            raise ValueError("Choose a backup location different from the live database.")
        with closing(sqlite3.connect(path)) as target:
            self.connection.backup(target)

    def close(self) -> None:
        self.connection.close()
