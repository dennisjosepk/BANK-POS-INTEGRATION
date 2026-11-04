import json
from pathlib import Path
import tempfile
import unittest

from diagnostics import DiagnosticLog, config_fields, safe_fields
from pos import PosConfig, PosError


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = DiagnosticLog(self.root / "logs", "1.3.0")

    def tearDown(self):
        self.temp.cleanup()

    def test_safe_event_keeps_com_details_but_drops_personal_payment_and_path_values(self):
        secret = "CUSTOMER-PRIVATE-XYZ"
        self.log.record("pos_operation_error", port="COM3", baud_rate=115200, win_error=5,
                        error_code="connector_missing", payer=secret, notes=secret, amount_minor=10000,
                        pan="4111111111111111", card_last4="1234", raw_sdk=secret,
                        connector_path="C:\\Users\\PrivateUser\\connector.exe")
        text = self.log.path.read_text()
        self.assertIn('"port":"COM3"', text)
        self.assertIn('"win_error":5', text)
        for value in (secret, "4111111111111111", "PrivateUser", "amount_minor", "card_last4", "raw_sdk"):
            self.assertNotIn(value, text)

    def test_exception_records_code_and_type_without_message_or_full_path(self):
        try:
            raise PosError("Customer John; PAN 4111111111111111; PIN 9999; C:\\Users\\John", "connector_missing")
        except PosError as error:
            self.log.exception("pos_config_error", error, port="COM4")
        text = self.log.path.read_text()
        self.assertIn("connector_missing", text)
        self.assertIn("PosError", text)
        for value in ("John", "4111111111111111", "9999", "C:"):
            self.assertNotIn(value, text)

    def test_config_snapshot_does_not_include_supplier_identity_or_connector_path(self):
        config = PosConfig(supplier="Private supplier label", terminal_model="Private label", port="COM7",
                           connector_path="C:\\Users\\PrivateUser\\bank.exe", terminal_id="PRIVATE-TERMINAL")
        snapshot = config_fields(config)
        self.assertEqual(snapshot["port"], "COM7")
        self.assertTrue(snapshot["connector_configured"])
        self.assertNotIn("Private", json.dumps(snapshot))
        self.assertNotIn("PRIVATE-TERMINAL", json.dumps(snapshot))

    def test_rotation_is_bounded_and_recent_events_survive(self):
        log = DiagnosticLog(self.root / "rotating", "1.3.0", max_bytes=600, backups=2)
        for i in range(50):
            log.record("usb_probe_result", port="COM3", win_error=i, result_code="port_busy")
        files = list(log.directory.glob("receipt-recorder.log*"))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 600 for path in files))
        self.assertEqual(log.recent(1)[0]["win_error"], 49)

    def test_export_filters_tampered_or_unknown_log_content_again(self):
        self.log.record("pos_config_error", error_code="connector_missing", port="COM3")
        existing = json.loads(self.log.path.read_text().splitlines()[0])
        existing.update(raw_sdk="SECRET-SDK-PAYLOAD", payer="PRIVATE-CUSTOMER", pan="4111111111111111",
                        frames=[{"file": "C:\\Users\\PRIVATE-CUSTOMER\\app.py", "line": 10},
                                {"file": "app.py", "line": 123}, {"file": {}, "line": 1}])
        with self.log.path.open("a", encoding="utf-8") as output:
            output.write("RAW SECRET-SDK-PAYLOAD\n")
            output.write(json.dumps(existing) + "\n")
            output.write(json.dumps({"event": "PRIVATE-CUSTOMER"}) + "\n")
        destination = self.root / "support.txt"
        self.log.export(destination, snapshot={"port": "COM3", "payer": "PRIVATE-CUSTOMER"})
        text = destination.read_text(encoding="utf-8")
        for value in ("SECRET-SDK-PAYLOAD", "PRIVATE-CUSTOMER", "4111111111111111", "C:"):
            self.assertNotIn(value, text)
        self.assertIn("connector_missing", text)
        self.assertIn('"file":"app.py","line":123', text)
        self.assertIn('"port": "COM3"', text)

    def test_export_cannot_overwrite_logs_database_or_a_link_to_it(self):
        database = self.root / "receipts.sqlite3"
        database.write_bytes(b"keep-database")
        for output in (database, self.log.directory / "export.txt"):
            with self.assertRaises(ValueError):
                self.log.export(output, protected=[database])
        alias = self.root / "database-alias.txt"
        try:
            alias.hardlink_to(database)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                self.log.export(alias, protected=[database])
        self.assertEqual(database.read_bytes(), b"keep-database")

    def test_logging_io_failure_is_non_fatal(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("existing user file")
        log = DiagnosticLog(blocked, "1.3.0")
        self.assertFalse(log.record("app_start"))
        self.assertFalse(log.available)
        self.assertEqual(blocked.read_text(), "existing user file")

    def test_unexpected_attribute_types_and_strings_are_dropped(self):
        values = safe_fields(dict(error_code={"pan": "4111111111111111"}, port="CUSTOMER-NAME", win_error=True,
                                  ports=["COM3", "SECRET", 5], stage="CUSTOMER-NAME", frames=[{"file": {}, "line": 4}]))
        self.assertEqual(values, {"ports": ["COM3"], "frames": []})


if __name__ == "__main__":
    unittest.main()
