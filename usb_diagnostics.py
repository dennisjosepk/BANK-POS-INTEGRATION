"""Non-financial Windows COM access check. Never sends or reads serial bytes."""

from __future__ import annotations

import ctypes
import re
import sys

from pos import serial_ports


class SerialDCB(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("baud_rate", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint16), ("xon_limit", ctypes.c_uint16), ("xoff_limit", ctypes.c_uint16),
        ("byte_size", ctypes.c_uint8), ("parity", ctypes.c_uint8), ("stop_bits", ctypes.c_uint8),
        ("xon_char", ctypes.c_char), ("xoff_char", ctypes.c_char), ("error_char", ctypes.c_char),
        ("eof_char", ctypes.c_char), ("event_char", ctypes.c_char), ("reserved1", ctypes.c_uint16),
    ]


def _usb_identity(port):
    """Map the selected COM port to non-unique VID/PID values; never return serials."""
    try:
        import winreg
    except ImportError:
        return {}
    def children(key, limit):
        for index in range(limit):
            try:
                yield winreg.EnumKey(key, index)
            except OSError:
                return
    for bus in ("USB", "FTDIBUS"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Enum\\" + bus) as root:
                for device in children(root, 4096):
                    match = re.search(r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})", device, re.I)
                    if not match:
                        continue
                    try:
                        with winreg.OpenKey(root, device) as device_key:
                            for instance in children(device_key, 256):
                                try:
                                    with winreg.OpenKey(device_key, instance + "\\Device Parameters") as parameters:
                                        value, _ = winreg.QueryValueEx(parameters, "PortName")
                                        if isinstance(value, str) and value.upper() == port:
                                            return dict(usb_vid=match.group(1).upper(), usb_pid=match.group(2).upper())
                                except OSError:
                                    continue
                    except OSError:
                        continue
        except OSError:
            continue
    return {}


def _open_windows_port(port):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.GetCommState.argtypes = [ctypes.c_void_p, ctypes.POINTER(SerialDCB)]
    kernel.GetCommState.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    # Exclusive access detects a port held by another application. No SetCommState,
    # ReadFile, WriteFile, test bytes or purchase/cancel commands are used.
    handle = kernel.CreateFileW("\\\\.\\" + port, 0xC0000000, 0, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        code = {2: "port_missing", 3: "port_missing", 5: "port_access_denied", 32: "port_busy",
                1167: "port_missing"}.get(error, "port_open_failed")
        return dict(result_code=code, win_error=error, serial_settings_read=False)
    try:
        dcb = SerialDCB()
        dcb.length = ctypes.sizeof(dcb)
        if not kernel.GetCommState(handle, ctypes.byref(dcb)):
            return dict(result_code="serial_query_failed", win_error=ctypes.get_last_error(), serial_settings_read=False)
        parity = {0: "None", 1: "Odd", 2: "Even", 3: "Mark", 4: "Space"}.get(dcb.parity)
        stop = {0: "1", 1: "1.5", 2: "2"}.get(dcb.stop_bits)
        flow = "RTS/CTS" if dcb.flags & 4 else "XON/XOFF" if dcb.flags & (256 | 512) else "None"
        return dict(result_code="port_opened", serial_settings_read=True, host_baud_rate=int(dcb.baud_rate),
                    host_data_bits=int(dcb.byte_size), host_parity=parity, host_stop_bits=stop,
                    host_flow_control=flow, host_flags=int(dcb.flags))
    finally:
        kernel.CloseHandle(handle)


def inspect_com_port(port, *, allow_open=True):
    port = port.strip().upper() if isinstance(port, str) else ""
    if not re.fullmatch(r"COM[1-9][0-9]{0,3}", port):
        return dict(result_code="port_invalid", port_present=False)
    ports = serial_ports()
    result = dict(port=port, ports=ports, port_count=len(ports), port_present=port in ports)
    if sys.platform != "win32":
        return {**result, "result_code": "platform_unsupported"}
    if port in ports:
        identity = _usb_identity(port)
        if identity:
            result.update(identity, usb_identity_source="registry_port_mapping")
    if not allow_open:
        return {**result, "result_code": "skipped_pending"}
    if port not in ports:
        return {**result, "result_code": "port_missing"}
    return {**result, **_open_windows_port(port)}


def probe_message(result):
    code = result.get("result_code")
    messages = {
        "port_invalid": "Select a valid COM port first, for example COM3.",
        "platform_unsupported": "USB/COM access checks are available in the Windows application.",
        "port_missing": "Windows did not find the selected COM port. Check the USB cable, ECR USB mode and supplier's USB driver.",
        "port_busy": "Windows reports that the port is in use. Wait for any other payment to finish, "
                     "then close the application using this COM port and check again.",
        "port_access_denied": "Windows denied access to the port. Another application may be using it, or the driver/access settings need attention.",
        "port_open_failed": "Windows could not open the COM port. Export the diagnostic log to share its Windows error number.",
        "serial_query_failed": "Windows opened the port but could not read its serial settings. Check the device driver and export the log.",
        "skipped_pending": "Port access was not tested because a bank payment is unconfirmed. Resolve that payment before opening its port.",
        "skipped_active": "Wait for the current POS operation to finish before testing USB/COM access.",
        "port_opened": "Windows detected and opened the COM port, then closed it. No serial bytes or payment commands were sent. "
                       "This confirms Windows port access only; an ECR connector is still needed to communicate with the bank POS.",
    }
    text = messages.get(code, "The COM check could not be completed. Export the diagnostic log.")
    if type(result.get("win_error")) is int:
        text += f"\n\nWindows error number: {result['win_error']}"
    return text
