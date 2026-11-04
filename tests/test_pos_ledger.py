import csv
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ledger import Ledger
from pos import BANK_METHOD, PosResult, parse_payment_result
from receipt_printing import FIELD_LABELS, ReceiptDesign, render_receipts_html


class PosLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "receipts.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.set_currency("SAR")
        self.entry = dict(received_date=date.today().isoformat(), amount="100.25", payer="Customer",
                          method=BANK_METHOD, reference="INV-1", notes="For invoice 1", payer_phone="0501234567", payer_address="Riyadh")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def approval(self, attempt_id, **changes):
        values = dict(request_id=attempt_id, status="approved", provider_id="fixture-provider", amount_minor=10025,
                      currency="SAR", transaction_id="TX-" + attempt_id, card_brand="Visa", card_last4="0012",
                      authorization_code="A123", terminal_id="TERM1")
        values.update(changes)
        return PosResult(**values)

    def test_method_cannot_bypass_bank_approval(self):
        with self.assertRaises(ValueError):
            self.ledger.add(**self.entry)
        self.assertEqual(self.ledger.receipts(), [])

    def test_approval_saves_once_with_card_metadata_and_print_and_report(self):
        attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        self.assertEqual(self.ledger.receipts(), [])
        result = self.approval(attempt["id"])
        receipt_id = self.ledger.finish_pos_payment(result)
        self.assertEqual(self.ledger.finish_pos_payment(result), receipt_id)
        self.assertEqual(len(self.ledger.receipts()), 1)
        row = self.ledger.receipt(receipt_id)
        self.assertEqual(row["card_brand"], "Visa")
        self.assertEqual(row["card_last4"], "0012")
        self.assertEqual(row["payer_phone"], "0501234567")
        self.assertEqual(self.ledger.pending_pos(), [])
        html = render_receipts_html([row], "SAR", ReceiptDesign())
        for text in ("Visa", "**** 0012", result.transaction_id, "A123", "TERM1"):
            self.assertIn(text, html)
        report = Path(self.temp.name) / "report.csv"
        self.ledger.export_csv(report)
        with report.open(encoding="utf-8-sig", newline="") as source:
            exported = list(csv.DictReader(source))[0]
        self.assertEqual(exported["Card brand"], "Visa")
        self.assertEqual(exported["Card last 4"], "**** 0012")
        self.assertEqual(exported["Bank transaction ID"], result.transaction_id)

    def test_confirmed_cancel_and_decline_never_create_receipts(self):
        for status in ("cancelled", "declined"):
            attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
            result = PosResult(attempt["id"], status, "fixture-provider", 10025)
            self.assertIsNone(self.ledger.finish_pos_payment(result))
            self.assertIsNone(self.ledger.finish_pos_payment(result))
            self.assertEqual(self.ledger.receipts(), [])
            self.assertEqual(self.ledger.pending_pos(), [])

    def test_unknown_and_restart_block_duplicate_charges_until_resolved(self):
        attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        self.ledger.finish_pos_payment(PosResult(attempt["id"], "unknown", "fixture-provider", 10025))
        self.ledger.close()
        self.ledger = Ledger(self.path)
        with self.assertRaises(ValueError):
            self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        self.assertEqual(self.ledger.receipts(), [])
        receipt_id = self.ledger.finish_pos_payment(self.approval(attempt["id"]))
        self.assertEqual(self.ledger.receipt(receipt_id)["reference"], "INV-1")
        self.assertEqual(self.ledger.pending_pos(), [])

    def test_another_app_instance_cannot_start_a_second_pending_payment(self):
        self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        second = Ledger(self.path)
        try:
            with self.assertRaises(ValueError):
                second.begin_pos_payment(self.entry, "fixture-provider")
        finally:
            second.close()

    def test_response_amount_currency_and_provider_must_match(self):
        attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        for changes in ({"amount_minor": 10026}, {"currency": "USD"}, {"provider_id": "different-provider"},
                        {"transaction_id": ""}, {"request_id": "other-request"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ledger.finish_pos_payment(self.approval(attempt["id"], **changes))
        self.assertEqual(self.ledger.receipts(), [])
        self.assertEqual(len(self.ledger.pending_pos()), 1)

    def test_duplicate_provider_transaction_is_not_counted_twice(self):
        first = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        self.ledger.finish_pos_payment(self.approval(first["id"], transaction_id="SAME-TX"))
        second = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.finish_pos_payment(self.approval(second["id"], transaction_id="SAME-TX"))
        self.assertEqual(len(self.ledger.receipts()), 1)
        self.assertEqual(len(self.ledger.pending_pos()), 1)

    def test_failed_receipt_write_keeps_attempt_pending_for_reconciliation(self):
        attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        from unittest.mock import patch
        with patch.object(self.ledger, "_insert_receipt", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.ledger.finish_pos_payment(self.approval(attempt["id"]))
        self.assertEqual(len(self.ledger.pending_pos()), 1)
        self.assertEqual(self.ledger.receipts(), [])

    def test_full_pan_and_unrecognized_response_data_are_never_stored(self):
        attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        packet = dict(request_id=attempt["id"], status="approved", amount_minor=10025, currency="SAR",
                      transaction_id="TX-SAFE", card_brand="MADA", card_last4="4111111111111111",
                      pan="4111111111111111", cvv="999", track_data="SENSITIVE-TRACK")
        result = parse_payment_result(packet, attempt["id"], 10025, "SAR", "fixture-provider")
        self.ledger.finish_pos_payment(result)
        rows = str([dict(row) for row in self.ledger.receipts()])
        stored = rows + self.ledger.pos_attempt(attempt["id"])["result_json"]
        self.assertNotIn(packet["pan"], stored)
        self.assertNotIn(packet["track_data"], stored)
        self.assertNotIn('"cvv"', stored)
        self.assertEqual(self.ledger.receipts()[0]["card_last4"], "")
        self.assertEqual(self.ledger.receipts()[0]["card_brand"], "mada")

    def test_saved_old_print_design_gains_card_fields_without_losing_customization(self):
        old_keys = [key for key in FIELD_LABELS if not key.startswith("pos_") and key not in ("card_brand", "card_last4")]
        old = dict(merchant_name="My Shop", field_order=old_keys, labels={key: FIELD_LABELS[key] for key in old_keys})
        old["labels"]["payer"] = "Customer name"
        restored = ReceiptDesign.from_json(json.dumps(old))
        self.assertEqual(restored.merchant_name, "My Shop")
        self.assertEqual(restored.labels["payer"], "Customer name")
        self.assertEqual(set(restored.field_order), set(FIELD_LABELS))

    def test_conflicting_final_results_remain_visible_for_reconciliation(self):
        attempt = self.ledger.begin_pos_payment(self.entry, "fixture-provider")
        approved = self.approval(attempt["id"])
        receipt_id = self.ledger.finish_pos_payment(approved)
        with self.assertRaises(ValueError):
            self.ledger.finish_pos_payment(PosResult(attempt["id"], "cancelled", "fixture-provider"))
        self.assertEqual(len(self.ledger.pending_pos()), 1)
        self.assertEqual(self.ledger.finish_pos_payment(approved), receipt_id)
        self.assertEqual(self.ledger.pending_pos(), [])
        self.assertEqual(len(self.ledger.receipts()), 1)


if __name__ == "__main__":
    unittest.main()
