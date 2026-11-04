import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ledger import Ledger
from receipt_printing import FIELD_LABELS, ReceiptDesign, load_logo, open_receipts, render_receipts_html


class PrintingTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(id=7, received_date=date.today().isoformat(), amount_minor=12550, payer="أحمد & Company",
                        payer_phone="0501234567", payer_address="First line\nSecond line", method="Cash",
                        reference="INV-7", notes="All notes <must> print", created_at="2026-11-04T08:00:00+00:00", voided_at=None)

    def test_80mm_document_includes_every_record_detail(self):
        design = ReceiptDesign(merchant_name="My Business", merchant_address="Business address", merchant_phone="1234")
        result = render_receipts_html([self.row], "SAR", design)
        for value in ("#7", self.row["received_date"], "SAR 125.50", "أحمد &amp; Company", "0501234567",
                      "First line\nSecond line", "Cash", "INV-7", "All notes &lt;must&gt; print",
                      self.row["created_at"], "Recorded", "My Business", "Business address", "1234"):
            self.assertIn(value, result)
        self.assertIn("size:80mm", result)
        self.assertIn('id="receipt-1"', result)
        self.assertIn('data-auto-print="no"', result)
        self.assertNotIn("REPRINT / COPY", result)

    def test_reprints_are_separate_pages_and_voids_are_unambiguous(self):
        second = dict(self.row, id=8, voided_at="2026-11-04T09:00:00+00:00")
        result = render_receipts_html([self.row, second], "SAR", ReceiptDesign(), reprint=True, auto_print=True)
        self.assertEqual(result.count('class="receipt"'), 2)
        self.assertEqual(result.count("REPRINT / COPY"), 2)
        self.assertIn("VOIDED RECEIPT", result)
        self.assertIn("VOIDED — excluded from totals", result)
        self.assertIn(second["voided_at"], result)
        self.assertIn("@page receipt2", result)
        self.assertIn('data-auto-print="yes"', result)

    def test_custom_labels_order_and_untrusted_text_are_safe(self):
        design = ReceiptDesign(title="<script>alert(1)</script>", footer="Line 1\nLine 2")
        design.field_order.reverse()
        design.labels["payer"] = "Customer <name>"
        result = render_receipts_html([dict(self.row, notes='<img src=x onerror="alert(2)">')], "SAR", design)
        self.assertNotIn("<script>alert(1)</script>", result)
        self.assertNotIn('<img src=x', result)
        self.assertIn("Customer &lt;name&gt;", result)
        self.assertIn("Line 1\nLine 2", result)
        self.assertLess(result.index("Voided at (UTC):"), result.index("Receipt no.:"))

    def test_design_round_trip_and_invalid_layouts(self):
        design = ReceiptDesign(merchant_name="My business", font_size=12, margin_mm=4)
        self.assertEqual(ReceiptDesign.from_json(design.to_json()), design)
        for invalid in (replace(design, font_size=0), replace(design, margin_mm=20),
                        replace(design, font_family="Arial; color:red"), replace(design, field_order=["id"]),
                        replace(design, field_order=["id"] * len(FIELD_LABELS)), replace(design, logo_data="data:text/html;base64,abc")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                invalid.validate()
        with self.assertRaises(ValueError):
            ReceiptDesign.from_json(json.dumps({"unknown": "value"}))

    def test_print_file_opens_browser_and_retains_all_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("receipt_printing.webbrowser.open", return_value=True) as browser:
                output = open_receipts([self.row], "SAR", ReceiptDesign(), Path(folder))
            browser.assert_called_once_with(output.resolve().as_uri(), new=2)
            self.assertIn("INV-7", output.read_text(encoding="utf-8"))
            with patch("receipt_printing.webbrowser.open", return_value=False), self.assertRaises(OSError):
                open_receipts([self.row], "SAR", ReceiptDesign(), Path(folder))

    def test_logo_is_embedded_and_saved_in_database_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            logo = Path(folder) / "logo.gif"
            logo.write_bytes(b"GIF89a" + b"\x00" * 10)
            design = ReceiptDesign(logo_data=load_logo(logo))
            self.assertIn(design.logo_data, render_receipts_html([self.row], "SAR", design))
            ledger = Ledger(Path(folder) / "book.sqlite3")
            ledger.set_preference("receipt_design", design.to_json())
            backup = Path(folder) / "backup.sqlite3"
            ledger.backup(backup)
            ledger.close()
            restored = Ledger(backup)
            try:
                self.assertEqual(ReceiptDesign.from_json(restored.preference("receipt_design")), design)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
