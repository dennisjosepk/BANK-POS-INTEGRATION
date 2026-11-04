import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ledger import Ledger, format_amount, parse_amount


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "receipts.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.set_currency("SAR")
        self.today = date.today().isoformat()

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_exact_amounts_persist_across_restart(self):
        first = self.ledger.add(self.today, "0.10", "First customer")
        second = self.ledger.add(self.today, "0.20", "Second customer", "Card (manual entry)")
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.currency, "SAR")
        self.assertEqual(self.ledger.totals(), (30, 30, 2))
        self.assertEqual([row["id"] for row in self.ledger.receipts()], [second, first])
        self.assertEqual(format_amount(30, "SAR"), "0.30")

    def test_invalid_input_never_creates_receipts(self):
        for amount in ("", "0", "-1", "NaN", "Infinity", "1e2", "1,000", "+2", "1.001", "1.",
                       "999999999999999999999999999999999"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.ledger.add(self.today, amount)
        for day in ("2026-02-30", "03/11/2026", "20261103", (date.today() + timedelta(days=1)).isoformat()):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.ledger.add(day, "1.00")
        with self.assertRaises(ValueError):
            self.ledger.add(self.today, "1.00", method="Approved")
        with self.assertRaises(ValueError):
            self.ledger.add(self.today, "1.00", notes="x" * 1001)
        self.assertEqual(self.ledger.receipts(), [])

    def test_void_preserves_history_and_excludes_totals(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        old = self.ledger.add(yesterday, "10")
        self.ledger.add(self.today, "25.50")
        self.assertEqual(self.ledger.totals(), (2550, 3550, 2))
        self.ledger.void(old)
        self.assertEqual(self.ledger.totals(), (2550, 2550, 1))
        self.assertEqual(len(self.ledger.receipts()), 2)
        self.assertIsNotNone(self.ledger.receipts()[1]["voided_at"])
        with self.assertRaises(ValueError):
            self.ledger.void(old)

    def test_csv_round_trip_unicode_and_formula_protection(self):
        receipt = self.ledger.add(self.today, "1250.50", "أحمد", "Card (manual entry)",
                                  '=HYPERLINK("https://example.com")', '  +1+2\nSecond line, quoted "text"')
        self.ledger.void(receipt)
        path = Path(self.temp.name) / "export.csv"
        self.assertEqual(self.ledger.export_csv(path), 1)
        with path.open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(rows[0]["Amount"], "1250.50")
        self.assertEqual(rows[0]["Currency"], "SAR")
        self.assertEqual(rows[0]["Payer"], "أحمد")
        self.assertTrue(rows[0]["Reference"].startswith("'="))
        self.assertEqual(rows[0]["Notes"], "'+1+2\nSecond line, quoted \"text\"")
        self.assertEqual(rows[0]["Status"], "Voided")

    def test_database_backup_can_be_opened_as_a_ledger(self):
        self.ledger.add(self.today, "83.49", reference="T-1234")
        backup = Path(self.temp.name) / "backup.sqlite3"
        self.ledger.backup(backup)
        restored = Ledger(backup)
        try:
            self.assertEqual(restored.totals(), (8349, 8349, 1))
            self.assertEqual(restored.receipts()[0]["reference"], "T-1234")
            self.assertEqual(restored.currency, "SAR")
            self.assertEqual(restored.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            restored.close()
        with self.assertRaises(ValueError):
            self.ledger.backup(self.path)

    def test_currency_cannot_silently_relabel_existing_amounts(self):
        self.ledger.add(self.today, "12")
        with self.assertRaises(ValueError):
            self.ledger.set_currency("USD")
        self.assertEqual(self.ledger.currency, "SAR")

    def test_export_cannot_overwrite_live_database(self):
        self.ledger.add(self.today, "10.25")
        with self.assertRaises(ValueError):
            self.ledger.export_csv(self.path)
        self.assertEqual(self.ledger.totals()[1], 1025)

    def test_supported_precision(self):
        self.assertEqual(parse_amount("1.001", "KWD"), 1001)
        self.assertEqual(parse_amount("150", "JPY"), 150)
        self.assertEqual(format_amount(1001, "KWD"), "1.001")
        with self.assertRaises(ValueError):
            parse_amount("150.01", "JPY")


if __name__ == "__main__":
    unittest.main()
