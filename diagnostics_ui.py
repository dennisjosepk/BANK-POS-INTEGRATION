"""Diagnostic controls and a non-financial Windows USB/COM check."""

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from pos import serial_ports
from usb_diagnostics import inspect_com_port, probe_message


class UsbProbeWindow:
    def __init__(self, parent, port, allow_open, on_done, logger):
        self.events = queue.Queue()
        self.on_done, self.log = on_done, logger
        self.window = tk.Toplevel(parent)
        self.window.title("USB / COM diagnostic check")
        self.window.geometry("530x210")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.window.bell)
        body = ttk.Frame(self.window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Checking Windows port access…", font="TkHeadingFont").pack(anchor="w")
        self.spinner = ttk.Progressbar(body, mode="indeterminate", length=480)
        self.spinner.pack(fill="x", pady=18)
        self.spinner.start(12)
        ttk.Label(body, text="No serial bytes or payment commands are sent.\n"
                  "Waiting for the Windows driver to finish the check.", wraplength=480).pack(anchor="w")
        self.window.grab_set()
        def worker():
            try:
                result = inspect_com_port(port, allow_open=allow_open)
            except Exception as error:
                self.log.exception("usb_probe_error", error)
                result = dict(result_code="port_open_failed")
                number = getattr(error, "winerror", None)
                if type(number) is int:
                    result["win_error"] = number
            self.events.put(result)
        threading.Thread(target=worker, daemon=True).start()
        self.window.after(50, self.poll)

    def poll(self):
        try:
            result = self.events.get_nowait()
        except queue.Empty:
            self.window.after(50, self.poll)
            return
        self.spinner.stop()
        self.window.grab_release()
        self.window.destroy()
        self.on_done(result)


class DiagnosticsWindow:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("Connection diagnostics & logs")
        self.window.geometry("850x650")
        self.window.minsize(760, 580)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(4, weight=1)
        ttk.Label(self.window, text="Connection diagnostics", style="Title.TLabel").grid(sticky="w", padx=18, pady=(16, 10))
        ttk.Label(self.window, text="Use this check even when you have no SDK or provider connector. "
                  "It tests Windows COM access only; bank communication still needs the supplier's ECR protocol.",
                  wraplength=800).grid(row=1, sticky="w", padx=18, pady=(0, 12))
        controls = ttk.Frame(self.window, padding=(18, 0))
        controls.grid(row=2, sticky="ew")
        ttk.Label(controls, text="COM port").pack(side="left", padx=(0, 8))
        snapshot = app.diagnostic_settings()
        self.port = tk.StringVar(value=snapshot.get("port", ""))
        self.ports = ttk.Combobox(controls, textvariable=self.port, values=serial_ports(), width=13)
        self.ports.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Refresh ports", command=self.refresh_ports).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Check USB / COM", command=self.check).pack(side="left")
        self.status = tk.StringVar(value="Diagnostic logs are recorded automatically. No receipts, card data or raw SDK messages are included.")
        ttk.Label(self.window, textvariable=self.status, wraplength=800).grid(row=3, sticky="w", padx=18, pady=14)
        self.text = scrolledtext.ScrolledText(self.window, wrap="word", font="TkFixedFont", state="disabled", padx=10, pady=10)
        self.text.grid(row=4, sticky="nsew", padx=18)
        actions = ttk.Frame(self.window, padding=18)
        actions.grid(row=5, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Refresh log", command=self.refresh_log).grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Open log folder", command=self.open_folder).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(actions, text="Export diagnostic log", style="Save.TButton", command=self.export).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(actions, text="Close", command=self.close).grid(row=0, column=3)
        self.refresh_log()

    def refresh_ports(self):
        ports = serial_ports()
        self.ports.configure(values=ports)
        self.app.diagnostics.record("ports_detected", ports=ports, port_count=len(ports))
        self.status.set("Detected ports: " + (", ".join(ports) or "none. Check ECR USB mode, cable and USB driver."))
        self.refresh_log()

    def check(self):
        self.app.check_usb_port(self.port.get(), parent=self.window, after_done=self.checked)

    def checked(self, result):
        self.status.set(probe_message(result))
        self.refresh_log()

    def refresh_log(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for row in self.app.diagnostics.recent():
            fields = [f"{key}={value}" for key, value in row.items() if key not in ("time_utc", "event", "version", "session", "frames")]
            self.text.insert("end", f"{row['time_utc']}  {row['event']}\n  {'; '.join(fields)}\n\n")
        self.text.configure(state="disabled")
        self.text.see("end")

    def export(self):
        self.app.export_diagnostics(parent=self.window, port=self.port.get())
        self.refresh_log()

    def open_folder(self):
        try:
            self.app.diagnostics.directory.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(self.app.diagnostics.directory))
            else:
                messagebox.showinfo("Log folder", str(self.app.diagnostics.directory), parent=self.window)
        except OSError as error:
            self.app.diagnostics.exception("diagnostic_export_error", error)
            messagebox.showerror("Could not open folder", "The log folder is not accessible. Try Export diagnostic log.", parent=self.window)

    def close(self):
        if self.app.active_usb_probe:
            self.app.active_usb_probe.window.lift()
            return
        self.window.destroy()
