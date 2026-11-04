from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pos import BankConnector, PosConfig, PosError, new_request_id


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        helper = root / "fixture.exe"
        helper.write_bytes(b"Fixture only; not a shipped connector")
        self.config = PosConfig(connector_path=str(helper), port="COM3", provider_id="fixture-provider", timeout_seconds=15)
        self.config.verified_fingerprint = self.config.fingerprint()
        self.audit = root / "operations.txt"
        self.real_popen = subprocess.Popen

    def tearDown(self):
        self.temp.cleanup()

    def run_scenario(self, scenario, operation="purchase", cancel=None, progress=None):
        cancel = cancel or threading.Event()
        def launch(_args, **kwargs):
            return self.real_popen([sys.executable, str(Path(__file__).with_name("pos_fixture.py")), scenario, str(self.audit)], **kwargs)
        with patch("pos.subprocess.Popen", side_effect=launch):
            return BankConnector(self.config).run(operation, new_request_id(), 10000, "SAR", cancel, progress or (lambda _: None))

    def test_handshake_and_approved_payment_metadata(self):
        result = self.run_scenario("approved")
        self.assertEqual(result.status, "approved")
        self.assertEqual(result.card_brand, "Visa")
        self.assertEqual(result.card_last4, "1234")
        self.assertEqual(self.audit.read_text().splitlines(), ["hello", "purchase"])

    def test_check_connection_uses_handshake_not_a_charge(self):
        result = self.run_scenario("approved", operation="check_connection")
        self.assertEqual(result.status, "connected")
        self.assertEqual(self.audit.read_text().splitlines(), ["hello", "check_connection"])

    def test_status_check_does_not_repeat_purchase(self):
        result = self.run_scenario("approved", operation="status")
        self.assertEqual(result.status, "approved")
        self.assertEqual(self.audit.read_text().splitlines(), ["hello", "status"])

    def test_unsupported_helper_never_receives_purchase(self):
        with self.assertRaises(PosError):
            self.run_scenario("bad_protocol")
        self.assertEqual(self.audit.read_text().splitlines(), ["hello"])

    def test_cancel_before_send_never_starts_connector(self):
        cancel = threading.Event()
        cancel.set()
        with patch("pos.subprocess.Popen") as launch:
            result = BankConnector(self.config).run("purchase", new_request_id(), 10000, "SAR", cancel, lambda _: None)
        launch.assert_not_called()
        self.assertEqual(result.status, "cancelled")

    def test_stopping_status_lookup_never_marks_original_payment_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        result = self.run_scenario("approved", operation="status", cancel=cancel)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(self.audit.exists())

    def test_cancel_sends_request_and_waits_for_terminal_confirmation(self):
        cancel = threading.Event()
        def progress(stage):
            if stage == "waiting_card":
                cancel.set()
        result = self.run_scenario("wait_cancel", cancel=cancel, progress=progress)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.audit.read_text().splitlines(), ["hello", "purchase", "cancel"])

    def test_terminal_cancel_and_decline_propagate(self):
        self.assertEqual(self.run_scenario("terminal_cancel").status, "cancelled")
        self.assertEqual(self.run_scenario("declined").status, "declined")

    def test_late_approval_is_not_mislabelled_as_cancelled(self):
        cancel = threading.Event()
        result = self.run_scenario("late_approval", cancel=cancel,
                                   progress=lambda stage: cancel.set() if stage == "waiting_card" else None)
        self.assertEqual(result.status, "approved")

    def test_cancel_acknowledgement_without_final_result_is_unknown(self):
        cancel = threading.Event()
        result = self.run_scenario("cancel_ack_only", cancel=cancel,
                                   progress=lambda stage: cancel.set() if stage == "waiting_card" else None)
        self.assertEqual(result.status, "unknown")

    def test_disconnect_wrong_amount_and_wrong_request_are_unknown(self):
        for scenario in ("disconnect", "wrong_amount", "wrong_id"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.run_scenario(scenario).status, "unknown")

    def test_timeout_requests_cancel_but_does_not_claim_cancellation(self):
        real_clock = time.monotonic
        advance = [0]
        def clock():
            if advance[0]:
                advance[0] += 20
            return real_clock() + advance[0]
        def progress(stage):
            if stage == "processing":
                advance[0] = 20
        with patch("pos.time.monotonic", side_effect=clock):
            result = self.run_scenario("timeout", progress=progress)
        self.assertEqual(result.status, "unknown")

    def test_changed_connector_requires_a_new_connection_check(self):
        Path(self.config.connector_path).write_bytes(b"changed")
        with self.assertRaises(PosError):
            self.config.validate()

    def test_approval_from_a_different_configured_terminal_is_unconfirmed(self):
        self.config.terminal_id = "OTHER-TERMINAL"
        self.config.verified_fingerprint = self.config.fingerprint()
        self.assertEqual(self.run_scenario("approved").status, "unknown")


if __name__ == "__main__":
    unittest.main()
