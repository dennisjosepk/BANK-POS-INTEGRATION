import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import ReceiptApp
from ledger import Ledger
from pos import BANK_METHOD, PosConfig, PosError


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "Requires a desktop or Xvfb")
class DiagnosticDesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.temp = tempfile.TemporaryDirectory()
        self.release = threading.Event()
        self.ledger = Ledger(Path(self.temp.name) / "receipts.sqlite3")
        self.ledger.set_currency("SAR")
        self.ledger.add("2026-11-03", "10", "PRIVATE-CUSTOMER", reference="PRIVATE-INVOICE", notes="PRIVATE-NOTE")
        self.patches = []
        for name in ("showinfo", "showwarning", "showerror"):
            item = patch("app.messagebox." + name)
            setattr(self, name, item.start())
            self.patches.append(item)
        self.root = tk.Tk()
        self.app = ReceiptApp(self.root, self.ledger)
        with patch("pos_ui.serial_ports", return_value=["COM3"]):
            self.app.open_pos_setup()
        self.setup = self.app.pos_setup
        self.setup.variables["port"].set("COM3")
        self.root.update()

    def tearDown(self):
        self.release.set()
        if self.app.active_usb_probe:
            self.pump()
        self.app.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def pump(self):
        deadline = time.monotonic() + 5
        while self.app.active_usb_probe and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertIsNone(self.app.active_usb_probe)

    def export_log(self):
        destination = Path(self.temp.name) / "support.txt"
        with patch("app.filedialog.asksaveasfilename", return_value=str(destination)):
            self.app.export_diagnostics()
        return destination.read_text(encoding="utf-8")

    def test_usb_check_and_export_work_without_any_bank_connector(self):
        self.assertEqual(self.setup.variables["connector_path"].get(), "")
        result = dict(port="COM3", ports=["COM3"], port_present=True, result_code="port_opened",
                      serial_settings_read=True, host_baud_rate=115200, host_data_bits=8)
        with patch("diagnostics_ui.inspect_com_port", return_value=result) as inspect:
            self.setup.check_usb()
            self.pump()
        inspect.assert_called_once_with("COM3", allow_open=True)
        self.assertIn("Windows", self.setup.connection_status.get())
        text = self.export_log()
        self.assertIn("port_opened", text)
        self.assertIn("COM3", text)
        for value in ("PRIVATE-CUSTOMER", "PRIVATE-INVOICE", "PRIVATE-NOTE", self.temp.name):
            self.assertNotIn(value, text)
        self.assertEqual(len(self.ledger.receipts()), 1)
        self.assertEqual(self.ledger.pending_pos(), [])

    def test_bank_check_missing_connector_is_logged_before_starting_a_payment(self):
        self.setup.check()
        self.showerror.assert_called_once()
        self.assertIsNone(self.app.active_pos)
        text = self.export_log()
        self.assertIn("connector_missing", text)
        self.assertIn("pos_config_error", text)
        self.assertEqual(self.ledger.pending_pos(), [])

    def test_device_settings_save_without_connector_and_remain_unverified(self):
        self.setup.variables["terminal_model"].set("A920PRO")
        self.setup.save()
        self.showerror.assert_not_called()
        saved = PosConfig.from_json(self.ledger.preference("pos_config"))
        self.assertEqual(saved.terminal_model, "A920PRO")
        self.assertEqual(saved.port, "COM3")
        self.assertEqual(saved.provider_id, "")
        self.assertEqual(saved.verified_fingerprint, "")
        self.assertIn("saved for diagnostics", self.setup.connection_status.get())
        event = self.app.diagnostics.recent(1)[0]
        self.assertEqual(event["event"], "pos_config_saved")
        self.assertFalse(event["settings_checked"])
        self.assertNotIn("error_code", event)
        with self.assertRaises(PosError):
            saved.validate()
        self.setup.close()
        with patch("pos_ui.serial_ports", return_value=["COM3"]):
            self.app.open_pos_setup()
        self.assertEqual(self.app.pos_setup.variables["terminal_model"].get(), "A920PRO")
        self.assertEqual(self.app.pos_setup.variables["port"].get(), "COM3")
        self.app.fields["method"].set(BANK_METHOD)
        self.app.fields["amount"].set("100")
        self.app.save()
        self.assertIsNone(self.app.active_pos)
        self.assertEqual(self.ledger.pending_pos(), [])
        self.assertEqual(len(self.ledger.receipts()), 1)

    def test_model_only_preferences_can_be_saved_before_a_port_is_selected(self):
        self.setup.variables["terminal_model"].set("A920PRO")
        self.setup.variables["port"].set("")
        self.setup.save()
        self.showerror.assert_not_called()
        saved = PosConfig.from_json(self.ledger.preference("pos_config"))
        saved.validate_settings(require_port=False)
        self.assertEqual(saved.terminal_model, "A920PRO")
        with self.assertRaises(PosError):
            saved.validate()

    def test_invalid_serial_preferences_still_do_not_save(self):
        self.setup.variables["baud_rate"].set("invalid")
        self.setup.save()
        self.showerror.assert_called_once()
        self.assertEqual(self.ledger.preference("pos_config"), "")

    def test_windows_port_busy_error_can_be_exported(self):
        with patch("diagnostics_ui.inspect_com_port", return_value=dict(port="COM3", result_code="port_busy", win_error=32)):
            self.setup.check_usb()
            self.pump()
        text = self.export_log()
        self.assertIn('"win_error":32', text)
        self.assertIn("port_busy", text)

    def test_ui_exception_message_is_not_written_to_log_or_support_popup(self):
        error = sqlite3.OperationalError("SELECT PRIVATE-CUSTOMER; PAN 4111111111111111")
        self.app.report_ui_error(type(error), error, None)
        text = self.export_log()
        self.assertIn("ui_error", text)
        self.assertIn("OperationalError", text)
        self.assertNotIn("PRIVATE-CUSTOMER", text)
        self.assertNotIn("4111111111111111", text)

    def test_bank_payment_cannot_start_while_usb_check_holds_the_port(self):
        def slow_probe(*args, **kwargs):
            self.release.wait(3)
            return dict(port="COM3", result_code="port_opened")
        with patch("diagnostics_ui.inspect_com_port", side_effect=slow_probe):
            self.setup.check_usb()
            self.app.fields["method"].set(BANK_METHOD)
            self.app.fields["amount"].set("100")
            self.app.save()
            self.assertIsNone(self.app.active_pos)
            self.assertEqual(self.ledger.pending_pos(), [])
            self.release.set()
            self.pump()

    def test_diagnostic_port_access_is_skipped_for_an_unconfirmed_payment(self):
        self.ledger.begin_pos_payment(dict(received_date="2026-11-03", amount="10", method=BANK_METHOD), "fixture-provider")
        with patch("diagnostics_ui.inspect_com_port", return_value=dict(port="COM3", result_code="skipped_pending")) as inspect:
            self.setup.check_usb()
            self.pump()
        inspect.assert_called_once_with("COM3", allow_open=False)
        self.assertEqual(len(self.ledger.pending_pos()), 1)


if __name__ == "__main__":
    unittest.main()
