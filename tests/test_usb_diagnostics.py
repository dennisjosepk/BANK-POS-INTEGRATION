import ctypes
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from usb_diagnostics import SerialDCB, _open_windows_port, _usb_identity, inspect_com_port


class UsbDiagnosticTests(unittest.TestCase):
    def test_dcb_layout_matches_the_windows_28_byte_abi(self):
        self.assertEqual(ctypes.sizeof(SerialDCB), 28)
        self.assertEqual(SerialDCB.byte_size.offset, 18)

    def test_usb_identification_returns_vid_pid_without_unique_serial(self):
        root_path = r"SYSTEM\CurrentControlSet\Enum\USB"
        device_path = root_path + r"\VID_1A86&PID_7523"
        parameter_path = device_path + r"\PRIVATE-SERIAL-123456\Device Parameters"
        class Key:
            def __init__(self, path): self.path = path
            def __enter__(self): return self
            def __exit__(self, *args): pass
        def open_key(base, path):
            full = (base.path + "\\" if isinstance(base, Key) else "") + path
            if full not in (root_path, device_path, parameter_path):
                raise OSError("not found")
            return Key(full)
        def enum_key(key, index):
            if index != 0:
                raise OSError("no more")
            if key.path == root_path:
                return "VID_1A86&PID_7523"
            if key.path == device_path:
                return "PRIVATE-SERIAL-123456"
            raise OSError("no more")
        fake = SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=open_key, EnumKey=enum_key,
                               QueryValueEx=lambda key, name: ("COM3", 1))
        with patch.dict(sys.modules, {"winreg": fake}):
            identity = _usb_identity("COM3")
            other = _usb_identity("COM9")
        self.assertEqual(identity, {"usb_vid": "1A86", "usb_pid": "7523"})
        self.assertEqual(other, {})
        self.assertNotIn("PRIVATE-SERIAL", str(identity))

    def test_missing_port_never_opens_another_device(self):
        with patch("usb_diagnostics.sys.platform", "win32"), patch("usb_diagnostics.serial_ports", return_value=["COM4"]), \
                patch("usb_diagnostics._open_windows_port") as opener:
            result = inspect_com_port("COM3")
        self.assertEqual(result["result_code"], "port_missing")
        opener.assert_not_called()

    def test_typed_port_is_normalized_before_access(self):
        with patch("usb_diagnostics.sys.platform", "win32"), patch("usb_diagnostics.serial_ports", return_value=["COM3"]), \
                patch("usb_diagnostics._open_windows_port", return_value=dict(result_code="port_opened")) as opener:
            result = inspect_com_port(" com3 ")
        opener.assert_called_once_with("COM3")
        self.assertEqual(result["port"], "COM3")

    def test_pending_payment_prevents_port_access(self):
        with patch("usb_diagnostics.sys.platform", "win32"), patch("usb_diagnostics.serial_ports", return_value=["COM3"]), \
                patch("usb_diagnostics._open_windows_port") as opener:
            result = inspect_com_port("COM3", allow_open=False)
        self.assertEqual(result["result_code"], "skipped_pending")
        opener.assert_not_called()

    def test_windows_access_denied_error_is_reported_as_a_number(self):
        kernel = MagicMock()
        kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
        with patch("usb_diagnostics.ctypes.WinDLL", return_value=kernel, create=True), \
                patch("usb_diagnostics.ctypes.get_last_error", return_value=5, create=True):
            result = _open_windows_port("COM3")
        self.assertEqual(result["result_code"], "port_access_denied")
        self.assertEqual(result["win_error"], 5)
        kernel.GetCommState.assert_not_called()

    def test_success_reads_host_settings_and_closes_handle_without_sending_bytes(self):
        kernel = MagicMock()
        kernel.CreateFileW.return_value = 1234
        def read_state(handle, pointer):
            self.assertEqual(handle, 1234)
            dcb = ctypes.cast(pointer, ctypes.POINTER(SerialDCB)).contents
            self.assertEqual(dcb.length, 28)
            dcb.baud_rate, dcb.byte_size, dcb.parity, dcb.stop_bits = 9600, 8, 0, 0
            return 1
        kernel.GetCommState.side_effect = read_state
        with patch("usb_diagnostics.ctypes.WinDLL", return_value=kernel, create=True):
            result = _open_windows_port("COM12")
        self.assertEqual(kernel.CreateFileW.call_args.args[0], r"\\.\COM12")
        self.assertEqual(result["result_code"], "port_opened")
        self.assertEqual(result["host_baud_rate"], 9600)
        kernel.CloseHandle.assert_called_once_with(1234)
        kernel.ReadFile.assert_not_called()
        kernel.WriteFile.assert_not_called()
        kernel.SetCommState.assert_not_called()

    def test_serial_settings_failure_still_closes_the_handle(self):
        kernel = MagicMock()
        kernel.CreateFileW.return_value, kernel.GetCommState.return_value = 1234, 0
        with patch("usb_diagnostics.ctypes.WinDLL", return_value=kernel, create=True), \
                patch("usb_diagnostics.ctypes.get_last_error", return_value=31, create=True):
            result = _open_windows_port("COM3")
        self.assertEqual(result["result_code"], "serial_query_failed")
        self.assertEqual(result["win_error"], 31)
        kernel.CloseHandle.assert_called_once_with(1234)


if __name__ == "__main__":
    unittest.main()
