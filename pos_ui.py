"""USB/COM configuration and responsive, cancellable POS operation windows."""

import json
from pathlib import Path
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ledger import format_amount
from pos import BAUD_RATES, PROGRESS, PosConfig, PosError, PosResult, serial_ports
from diagnostics import NullDiagnosticLog, attempt_tag, config_fields
from usb_diagnostics import probe_message


class PaymentProgress:
    def __init__(self, parent, connector, operation, request_id, amount_minor, currency, on_done, logger=None):
        self.connector, self.operation, self.request_id = connector, operation, request_id
        self.amount_minor, self.currency, self.on_done = amount_minor, currency, on_done
        self.log = logger or NullDiagnosticLog()
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.title("Bank POS payment" if operation == "purchase" else "Bank POS connection / status")
        self.window.geometry("560x280")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.request_cancel)
        self.window.bind("<Escape>", lambda _: self.request_cancel())
        body = ttk.Frame(self.window, padding=22)
        body.pack(fill="both", expand=True)
        caption = f"{currency} {format_amount(amount_minor, currency)}" if operation != "check_connection" else "Check terminal connection"
        ttk.Label(body, text=caption, font="TkHeadingFont").pack(anchor="w", pady=(0, 14))
        self.message = tk.StringVar(value=PROGRESS["connecting"])
        ttk.Label(body, textvariable=self.message, wraplength=510).pack(anchor="w")
        self.spinner = ttk.Progressbar(body, mode="indeterminate", length=510)
        self.spinner.pack(fill="x", pady=16)
        self.spinner.start(12)
        ttk.Label(body, text="Keep this window open until the terminal confirms the result.", wraplength=510).pack(anchor="w")
        self.cancel_button = ttk.Button(body, text="Cancel transaction" if operation == "purchase" else "Stop check",
                                        command=self.request_cancel)
        self.cancel_button.pack(anchor="e", pady=(14, 0))
        self.window.grab_set()

    def start(self):
        def worker():
            try:
                result = self.connector.run(self.operation, self.request_id, self.amount_minor, self.currency,
                                            self.cancel_event, lambda stage: self.events.put(("progress", stage)))
                self.events.put(("result", result))
            except PosError as error:
                self.log.exception("pos_operation_error", error, operation=self.operation, attempt_tag=attempt_tag(self.request_id))
                # BankConnector only raises before a financial request was sent.
                message = ("No payment was sent. Check the compatible bank connector and ECR/USB settings."
                           if self.operation == "purchase" else
                           "Connection/status was not confirmed. Any earlier unconfirmed payment remains unresolved.")
                self.events.put(("error", message))
            except Exception as error:
                self.log.exception("pos_operation_error", error, operation=self.operation, attempt_tag=attempt_tag(self.request_id))
                # Never interpret a worker failure as proof of a cancelled bank transaction.
                self.events.put(("result", PosResult(self.request_id, "unknown", self.connector.config.provider_id,
                                                     self.amount_minor, self.currency)))
        threading.Thread(target=worker, daemon=True).start()
        self.window.after(50, self.poll)

    def request_cancel(self):
        if self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.log.record("pos_ui_cancel", operation=self.operation, attempt_tag=attempt_tag(self.request_id), reason="user")
        self.cancel_button.state(["disabled"])
        self.message.set(PROGRESS["cancelling"] if self.operation == "purchase"
                         else "Stopping this check. An unconfirmed payment will remain unconfirmed.")

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                self.window.after(50, self.poll)
                return
            if kind == "progress":
                if not self.cancel_event.is_set():
                    self.message.set(PROGRESS.get(value, PROGRESS["processing"]))
                continue
            self.spinner.stop()
            self.window.grab_release()
            self.window.destroy()
            self.on_done(value if kind == "result" else None, value if kind == "error" else "", self.cancel_event.is_set())
            return


class PosSetup:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("Bank POS setup — USB / COM")
        self.window.geometry("930x790")
        self.window.minsize(880, 740)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(2, weight=1)
        try:
            self.saved = PosConfig.from_json(app.ledger.preference("pos_config"))
        except PosError:
            self.saved = PosConfig()
        self.checked_config = None
        self.variables = {}
        ttk.Label(self.window, text="Bank POS setup", style="Title.TLabel").grid(sticky="w", padx=18, pady=(14, 8))
        instructions = (
            "Enable ECR mode on the payment terminal and install its supplier's Windows USB/ECR driver. "
            "Select the COM port shown in Device Manager and the baud rate specified by that supplier.\n"
            "A compatible provider connector is required for the terminal's ECR protocol. "
            "A detected COM port alone does not confirm payment support."
        )
        ttk.Label(self.window, text=instructions, wraplength=880).grid(row=1, sticky="w", padx=18, pady=(0, 12))
        self.notebook = ttk.Notebook(self.window)
        self.notebook.grid(row=2, sticky="nsew", padx=18, pady=(0, 12))
        form = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(form, text="  USB / ECR connection  ")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        for key, caption, row, col in (
                ("supplier", "POS supplier / provider", 0, 0), ("terminal_model", "Terminal brand / model", 0, 2),
                ("port", "USB virtual COM port", 1, 0), ("baud_rate", "Baud rate", 1, 2),
                ("data_bits", "Data bits", 2, 0), ("parity", "Parity", 2, 2),
                ("stop_bits", "Stop bits", 3, 0), ("flow_control", "Flow control", 3, 2),
                ("terminal_id", "Terminal ID (if required)", 4, 0), ("timeout_seconds", "Payment timeout (seconds)", 4, 2),
                ("connector_path", "Provider connector (.exe)", 5, 0)):
            variable = tk.StringVar(value=str(getattr(self.saved, key)))
            self.variables[key] = variable
            ttk.Label(form, text=caption).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=7)
            if key == "port":
                widget = ttk.Combobox(form, textvariable=variable, values=serial_ports(), width=13)
                self.port_list = widget
            elif key == "baud_rate":
                widget = ttk.Combobox(form, textvariable=variable, values=BAUD_RATES, state="readonly", width=13)
            elif key in ("data_bits", "parity", "stop_bits", "flow_control"):
                choices = {"data_bits": (5, 6, 7, 8), "parity": ("None", "Even", "Odd", "Mark", "Space"),
                           "stop_bits": ("1", "1.5", "2"), "flow_control": ("None", "RTS/CTS", "XON/XOFF")}
                widget = ttk.Combobox(form, textvariable=variable, values=choices[key], state="readonly", width=13)
            else:
                widget = ttk.Entry(form, textvariable=variable, width=22)
            widget.grid(row=row, column=col + 1, columnspan=3 if key == "connector_path" else 1,
                        sticky="ew", padx=(0, 10), pady=7)
        controls = ttk.Frame(form)
        controls.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 12))
        ttk.Button(controls, text="Refresh COM ports", command=self.refresh_ports).pack(side="left")
        ttk.Button(controls, text="Choose connector...", command=self.choose_connector).pack(side="left", padx=8)
        ttk.Button(controls, text="Check bank connection", command=self.check).pack(side="left")
        ttk.Button(controls, text="Save settings", style="Save.TButton", command=self.save).pack(side="right")
        diagnostics = ttk.Frame(form)
        diagnostics.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ttk.Button(diagnostics, text="Check USB / COM", command=self.check_usb).pack(side="left")
        ttk.Button(diagnostics, text="Export diagnostic log", command=lambda: self.app.export_diagnostics(parent=self.window)).pack(
            side="left", padx=8)
        ttk.Button(diagnostics, text="View diagnostics", command=self.app.open_diagnostics).pack(side="left")
        self.connection_status = tk.StringVar(value="Connection not checked. Use Check connection to verify the terminal handshake.")
        ttk.Label(form, textvariable=self.connection_status, wraplength=830).grid(row=8, column=0, columnspan=4, sticky="w", pady=10)
        ttk.Label(form, text="No live supplier connector is included in this build. A connector must be implemented "
                  "for the supplier's documented ECR SDK/protocol before live payments can be enabled.", wraplength=830).grid(
            row=9, column=0, columnspan=4, sticky="w", pady=10)
        pending = ttk.LabelFrame(self.notebook, text="Pending / unconfirmed payments", padding=12)
        self.notebook.add(pending, text="  Pending payments  ")
        pending.columnconfigure(0, weight=1)
        pending.rowconfigure(1, weight=1)
        ttk.Label(pending, text="New bank payments are blocked while any earlier outcome is unconfirmed. "
                  "Check the same provider's payment status; do not charge the customer again.", wraplength=850).grid(sticky="w", pady=(0, 8))
        self.pending_tree = ttk.Treeview(pending, columns=("time", "amount", "customer", "status", "request"),
                                         show="headings", selectmode="browse", height=5)
        for key, caption, width in (("time", "Started (UTC)", 170), ("amount", "Amount", 105),
                                     ("customer", "Customer", 150), ("status", "Outcome", 100), ("request", "Request ID", 260)):
            self.pending_tree.heading(key, text=caption)
            self.pending_tree.column(key, width=width, minwidth=70)
        self.pending_tree.grid(row=1, sticky="nsew")
        scrollbar = ttk.Scrollbar(pending, orient="vertical", command=self.pending_tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.pending_tree.configure(yscrollcommand=scrollbar.set)
        buttons = ttk.Frame(pending)
        buttons.grid(row=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Refresh list", command=self.refresh_pending).pack(side="left")
        ttk.Button(buttons, text="Check selected payment status", command=self.check_pending).pack(side="left", padx=8)
        ttk.Button(self.window, text="Close", command=self.close).grid(row=3, sticky="e", padx=18, pady=(0, 14))
        self.refresh_pending()

    def config(self):
        values = {key: variable.get().strip() for key, variable in self.variables.items()}
        values["port"] = values["port"].upper()
        try:
            values["baud_rate"], values["timeout_seconds"] = int(values["baud_rate"]), int(values["timeout_seconds"])
            values["data_bits"] = int(values["data_bits"])
        except ValueError:
            raise PosError("Enter a valid baud rate and timeout in seconds.", "invalid_serial_parameters") from None
        result = PosConfig(**values)
        if self.checked_config:
            checked = self.checked_config
        else:
            checked = self.saved
        result.provider_id = checked.provider_id
        result.verified_fingerprint = checked.verified_fingerprint
        return result

    def refresh_ports(self):
        ports = serial_ports()
        self.app.diagnostics.record("ports_detected", ports=ports, port_count=len(ports))
        self.port_list.configure(values=ports)
        self.connection_status.set("Detected: " + ", ".join(ports) if ports
                                   else "No COM ports detected. Check the terminal's ECR USB driver and USB cable.")

    def choose_connector(self):
        path = filedialog.askopenfilename(parent=self.window, title="Choose a Receipt Recorder compatible provider connector",
                                          filetypes=[("Windows connector", "*.exe")])
        if path:
            self.variables["connector_path"].set(path)
            self.connection_status.set("Connector changed. Check the connection before taking payments.")

    def check(self):
        snapshot = config_fields({key: variable.get() for key, variable in self.variables.items()})
        self.app.diagnostics.record("pos_check_requested", **snapshot)
        try:
            config = self.config()
            config.validate(require_verified=False)
            config.provider_id = ""
            config.verified_fingerprint = ""
        except (PosError, OSError) as error:
            self.app.diagnostics.exception("pos_config_error", error, **snapshot)
            messagebox.showerror("Connection setup needed", str(error), parent=self.window)
            return
        def done(result, error, _cancelled):
            if result and result.status == "connected":
                config.provider_id = result.provider_id
                try:
                    config.verified_fingerprint = config.fingerprint()
                except PosError:
                    self.connection_status.set("The connector file changed during the check. Select it and check again.")
                    return
                self.checked_config = config
                self.app.diagnostics.record("pos_check_result", status="connected", **config_fields(config))
                self.connection_status.set(f"Terminal handshake confirmed by {result.provider_id} on {config.port}. Save settings to use it.")
            else:
                self.app.diagnostics.record("pos_check_result", status="not_confirmed")
                self.connection_status.set("Connection was not confirmed. No payment was sent.")
                if error:
                    messagebox.showwarning("Connection not confirmed", error, parent=self.window)
        self.app.start_pos_operation(config, "check_connection", None, done, parent=self.window)

    def check_usb(self):
        self.app.check_usb_port(self.variables["port"].get(), parent=self.window,
                               after_done=lambda result: self.connection_status.set(probe_message(result)))

    def save(self):
        try:
            config = self.config()
            config.validate()
            self.app.ledger.set_preference("pos_config", config.to_json())
            self.app.diagnostics.record("pos_config_saved", **config_fields(config))
            self.saved = config
            self.connection_status.set(f"Settings saved for {config.port}. Each payment still requires terminal approval.")
            self.app.update_pos_hint()
        except (ValueError, OSError, sqlite3.Error) as error:
            self.app.diagnostics.exception("pos_config_error", error, **config_fields({key: var.get() for key, var in self.variables.items()}))
            messagebox.showerror("Settings not saved", str(error), parent=self.window)

    def refresh_pending(self):
        for key in self.pending_tree.get_children():
            self.pending_tree.delete(key)
        for row in self.app.ledger.pending_pos():
            customer = json.loads(row["payload"]).get("payer", "")
            self.pending_tree.insert("", "end", iid=row["id"], values=(row["created_at"],
                                     f"{row['currency']} {format_amount(row['amount_minor'], row['currency'])}",
                                     customer, "Unconfirmed", row["id"]))

    def check_pending(self):
        selected = self.pending_tree.selection()
        if not selected:
            messagebox.showinfo("Choose payment", "Select an unconfirmed payment first.", parent=self.window)
            return
        self.app.check_pending_payment(selected[0], parent=self.window, after_done=self.refresh_pending)

    def close(self):
        if self.app.active_usb_probe:
            self.app.active_usb_probe.window.lift()
            return
        if self.app.active_pos:
            self.app.active_pos.window.lift()
            return
        self.window.destroy()
