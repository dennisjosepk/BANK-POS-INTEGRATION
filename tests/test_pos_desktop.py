import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app import ReceiptApp
from ledger import Ledger
from pos import BANK_METHOD, PosConfig, PosError, PosResult


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "Requires a desktop or Xvfb")
class PosDesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "receipts.sqlite3")
        self.ledger.set_currency("SAR")
        helper = Path(self.temp.name) / "fixture.exe"
        helper.write_bytes(b"Test fixture only")
        self.config = PosConfig(connector_path=str(helper), port="COM3", provider_id="fixture-provider")
        self.config.verified_fingerprint = self.config.fingerprint()
        self.ledger.set_preference("pos_config", self.config.to_json())
        self.mode, self.operations = "approved", []
        self.patchers = []
        for name in ("showinfo", "showwarning", "showerror"):
            item = patch("app.messagebox." + name)
            setattr(self, name, item.start())
            self.patchers.append(item)
        preview = patch("app.open_receipts")
        self.printing = preview.start()
        self.patchers.append(preview)
        owner = self
        class FakeConnector:
            def __init__(self, config, logger=None): self.config = config
            def run(self, operation, request_id, amount_minor, currency, cancel, progress):
                owner.operations.append(operation)
                progress("connecting")
                if operation == "check_connection":
                    return PosResult(request_id, "connected", "fixture-provider")
                if owner.mode == "error":
                    raise PosError("Fixture pre-send error")
                if owner.mode in ("wait_cancel", "late_approval"):
                    progress("waiting_card")
                    cancel.wait(3)
                    status = "approved" if owner.mode == "late_approval" else "cancelled"
                else:
                    status = {"terminal_cancel": "cancelled", "declined": "declined", "unknown": "unknown"}.get(owner.mode, "approved")
                return PosResult(request_id, status, "fixture-provider", amount_minor, currency,
                                 "TX-" + request_id, "mada", "4321", "A1", "TERM1")
        connector = patch("app.BankConnector", FakeConnector)
        connector.start()
        self.patchers.append(connector)
        self.root = tk.Tk()
        self.app = ReceiptApp(self.root, self.ledger)
        self.app.fields["method"].set(BANK_METHOD)
        self.app.fields["amount"].set("100")
        self.app.fields["payer"].set("Customer")
        self.root.update()

    def tearDown(self):
        if self.app.active_pos:
            self.app.active_pos.request_cancel()
            self.pump()
        self.app.close()
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def pump(self):
        deadline = time.monotonic() + 5
        while self.app.active_pos and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertIsNone(self.app.active_pos, "POS worker did not finish")

    def test_approval_is_saved_once_and_always_opens_print_preview(self):
        self.assertFalse(self.app.print_after_save.get())
        self.app.save()
        self.app.save()  # Repeated clicks while loading cannot start another charge.
        self.pump()
        self.assertEqual(self.operations, ["purchase"])
        self.assertEqual(len(self.ledger.receipts()), 1)
        row = self.ledger.receipts()[0]
        self.assertEqual(row["card_brand"], "mada")
        self.assertEqual(row["card_last4"], "4321")
        self.printing.assert_called_once()
        self.assertEqual(self.app.fields["amount"].get(), "")

    def test_cancellable_loader_keeps_ui_responsive_and_saves_nothing_on_cancel(self):
        self.mode = "wait_cancel"
        self.app.save()
        self.assertTrue(self.app.active_pos.window.winfo_exists())
        responsive = []
        self.root.after(0, lambda: responsive.append(True))
        self.root.update()
        self.assertEqual(responsive, [True])
        self.app.active_pos.cancel_button.invoke()
        self.pump()
        self.assertEqual(self.ledger.receipts(), [])
        self.assertEqual(self.ledger.pending_pos(), [])
        self.printing.assert_not_called()
        self.assertEqual(self.app.fields["amount"].get(), "100")

    def test_terminal_cancel_and_decline_stop_saving(self):
        for mode in ("terminal_cancel", "declined"):
            self.mode = mode
            self.app.save()
            self.pump()
            self.assertEqual(self.ledger.receipts(), [])
            self.assertEqual(self.ledger.pending_pos(), [])
        self.printing.assert_not_called()

    def test_unknown_blocks_recharge_and_status_recovery_preserves_unsaved_form(self):
        self.mode = "unknown"
        self.app.save()
        self.pump()
        self.assertEqual(len(self.ledger.pending_pos()), 1)
        self.assertEqual(self.ledger.receipts(), [])
        self.app.save()
        self.showerror.assert_called_once()
        self.assertEqual(self.operations, ["purchase"])
        attempt = self.ledger.pending_pos()[0]
        self.mode = "approved"
        self.app.fields["amount"].set("88")
        self.app.check_pending_payment(attempt["id"])
        self.pump()
        self.assertEqual(self.operations, ["purchase", "status"])
        self.assertEqual(len(self.ledger.receipts()), 1)
        self.assertEqual(self.app.fields["amount"].get(), "88")
        self.assertEqual(self.ledger.pending_pos(), [])

    def test_approval_that_wins_cancel_race_is_recorded_and_explained(self):
        self.mode = "late_approval"
        self.app.save()
        self.app.active_pos.request_cancel()
        self.pump()
        self.assertEqual(len(self.ledger.receipts()), 1)
        self.assertIn("approved", self.showwarning.call_args.args[1])
        self.printing.assert_called_once()

    def test_closing_main_window_requests_cancellation_instead_of_abandoning_payment(self):
        self.mode = "wait_cancel"
        self.app.save()
        self.app.close()
        self.assertTrue(self.root.winfo_exists())
        self.pump()
        self.assertEqual(self.ledger.receipts(), [])

    def test_setup_saves_serial_settings_after_confirmed_handshake(self):
        with patch("pos_ui.serial_ports", return_value=["COM3"]):
            self.app.open_pos_setup()
        setup = self.app.pos_setup
        self.root.update()
        setup.check()
        self.pump()
        setup.save()
        saved = PosConfig.from_json(self.ledger.preference("pos_config"))
        saved.validate()
        self.assertEqual(saved.port, "COM3")
        self.assertEqual(saved.provider_id, "fixture-provider")
        self.assertEqual(self.operations, ["check_connection"])
        self.assertEqual(self.ledger.receipts(), [])

    def test_without_connector_no_payment_or_false_receipt_is_created(self):
        self.ledger.set_preference("pos_config", "")
        self.app.save()
        self.assertIsNone(self.app.active_pos)
        self.assertEqual(self.operations, [])
        self.assertEqual(self.ledger.pending_pos(), [])
        self.assertEqual(self.ledger.receipts(), [])
        self.showerror.assert_called_once()

    def test_changed_connection_can_save_preferences_but_requires_a_new_check(self):
        with patch("pos_ui.serial_ports", return_value=["COM3", "COM9"]):
            self.app.open_pos_setup()
        setup = self.app.pos_setup
        setup.variables["port"].set("COM9")
        setup.save()
        self.showerror.assert_not_called()
        saved = PosConfig.from_json(self.ledger.preference("pos_config"))
        self.assertEqual(saved.port, "COM9")
        self.assertEqual(saved.provider_id, "")
        self.assertEqual(saved.verified_fingerprint, "")
        self.app.save()
        self.showerror.assert_called_once()
        self.assertEqual(self.operations, [])
        self.assertEqual(self.ledger.receipts(), [])

    def test_diagnostic_settings_cannot_overwrite_a_pending_payments_provider(self):
        original = self.ledger.preference("pos_config")
        self.ledger.begin_pos_payment(dict(received_date="2026-11-03", amount="100", method=BANK_METHOD), "fixture-provider")
        with patch("pos_ui.serial_ports", return_value=["COM3", "COM9"]):
            self.app.open_pos_setup()
        setup = self.app.pos_setup
        setup.variables["port"].set("COM9")
        setup.save()
        self.showerror.assert_called_once()
        self.assertEqual(self.ledger.preference("pos_config"), original)
        self.assertEqual(len(self.ledger.pending_pos()), 1)


if __name__ == "__main__":
    unittest.main()
