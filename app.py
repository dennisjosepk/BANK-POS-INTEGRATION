"""Receipt Recorder: local SAR receipts, history and 80 mm thermal printing."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import textwrap
import tkinter as tk
from datetime import date, datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from ledger import METHODS, Ledger, format_amount
from receipt_designer import ReceiptDesigner
from receipt_printing import ReceiptDesign, open_receipts, receipt_values, FIELD_LABELS
from pos import BANK_METHOD, BankConnector, PosConfig, PosError, PosResult, new_request_id
from pos_ui import PaymentProgress, PosSetup
from diagnostics import DiagnosticLog, attempt_tag, config_fields
from diagnostics_ui import DiagnosticsWindow, UsbProbeWindow
from usb_diagnostics import probe_message
from pos import serial_ports

APP_VERSION = "1.3.1"


def default_data_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ReceiptRecorder" / "receipts.sqlite3"


class ReceiptApp:
    def __init__(self, root: tk.Tk, ledger: Ledger, diagnostics=None):
        self.root, self.ledger = root, ledger
        self.diagnostics = diagnostics or DiagnosticLog(ledger.path.parent / "logs", APP_VERSION)
        if diagnostics is None:
            self.diagnostics.record("app_start")
        self.currency = ledger.currency
        if self.currency is None:
            raise ValueError("Choose a ledger currency before opening the receipt screen.")
        self.rows = {}
        self.checked_ids = set()
        self.active_filters = ("", "")
        self.designer_window = None
        self.pos_setup = None
        self.active_pos = None
        self.active_usb_probe = None
        self.diagnostics_window = None
        root.title(f"Receipt Recorder {APP_VERSION}")
        root.geometry("1140x800")
        root.minsize(1000, 720)
        root.configure(background="#f2f5fa")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.report_ui_error
        family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name, root).configure(family=family, size=10)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f5fa")
        style.configure("TLabel", background="#f2f5fa", foreground="#24334b", font=(family, 10))
        style.configure("Title.TLabel", font=(family, 23, "bold"))
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("Summary.TLabel", background="white", font=(family, 17, "bold"), foreground="#165c49")
        style.configure("TLabelframe", background="white", bordercolor="#dce3ed", relief="solid")
        style.configure("TLabelframe.Label", background="white", font=(family, 11, "bold"))
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=5)
        style.configure("TButton", padding=(12, 7), font=(family, 10))
        style.configure("TCheckbutton", background="#f2f5fa", font=(family, 10))
        style.configure("Card.TCheckbutton", background="white")
        style.configure("Save.TButton", foreground="white", background="#165c49")
        style.map("Save.TButton", background=[("active", "#104737")])
        style.configure("Treeview", rowheight=30, font=(family, 10), borderwidth=0)
        style.configure("Treeview.Heading", font=(family, 10, "bold"), background="#e8eef5", padding=(5, 8))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body = ttk.Frame(root, padding=20)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Receipt Recorder", style="Title.TLabel").grid(sticky="w")
        ttk.Button(header, text="Bank POS setup", command=self.open_pos_setup).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(header, text="Receipt designer · 80 mm", command=self.open_designer).grid(row=0, column=2)
        ttk.Button(header, text="Diagnostics", command=self.open_diagnostics).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(body, text="Record money received in Saudi riyals. Print and find your receipts.").grid(
            row=1, sticky="w", pady=(2, 14))
        summary = ttk.Frame(body)
        summary.grid(row=2, sticky="ew", pady=(0, 16))
        self.today_total, self.all_total, self.count = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for col, (caption, variable) in enumerate((("RECEIVED TODAY", self.today_total),
                                                  ("TOTAL RECEIVED · ALL DATES", self.all_total),
                                                  ("ACTIVE RECEIPTS · ALL DATES", self.count))):
            summary.columnconfigure(col, weight=1)
            panel = ttk.Frame(summary, style="Card.TFrame", padding=12)
            panel.grid(row=0, column=col, sticky="ew", padx=(0, 12 if col < 2 else 0))
            ttk.Label(panel, text=caption, style="Card.TLabel").pack(anchor="w")
            ttk.Label(panel, textvariable=variable, style="Summary.TLabel").pack(anchor="w", pady=(5, 0))
        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=3, sticky="nsew")
        self.new_tab = ttk.Frame(self.notebook, padding=16)
        self.history_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.new_tab, text="  New receipt  ")
        self.notebook.add(self.history_tab, text="  Receipt history & reprint  ")
        self.build_form()
        self.build_history()
        self.status = tk.StringVar(value="Ready. Receipts are saved automatically on this computer.")
        ttk.Label(body, textvariable=self.status, wraplength=1050).grid(row=4, sticky="w", pady=(10, 0))
        ttk.Label(body, text=f"Data: {ledger.path}", foreground="#65738a", wraplength=1050).grid(
            row=5, sticky="w", pady=(4, 0))
        self.refresh()
        if self.ledger.pending_pos():
            self.status.set("An earlier bank payment is unconfirmed. Open Bank POS setup → Pending payments before taking another card payment.")
        self.amount_entry.focus_set()

    def build_form(self):
        self.new_tab.columnconfigure(0, weight=1)
        form = ttk.LabelFrame(self.new_tab, text="New receipt", padding=16)
        form.grid(sticky="ew")
        for col in range(4):
            form.columnconfigure(col, weight=1, uniform="form")
        self.fields = {key: tk.StringVar(value=value) for key, value in (
            ("date", date.today().isoformat()), ("amount", ""), ("method", METHODS[0]),
            ("reference", ""), ("payer", ""), ("payer_phone", ""), ("payer_address", ""), ("notes", ""))}
        specifications = (("Received date (YYYY-MM-DD)", "date", 0, 0, 1),
                          (f"Amount ({self.currency}) *", "amount", 0, 1, 1),
                          ("Payment method *", "method", 0, 2, 1),
                          ("Invoice / reference (optional)", "reference", 0, 3, 1),
                          ("Received from · name / company", "payer", 2, 0, 2),
                          ("Phone (optional)", "payer_phone", 2, 2, 2),
                          ("Address (optional)", "payer_address", 4, 0, 4),
                          ("Notes (optional)", "notes", 6, 0, 4))
        for caption, key, row, col, span in specifications:
            ttk.Label(form, text=caption, style="Card.TLabel").grid(
                row=row, column=col, columnspan=span, sticky="w", padx=(0, 10), pady=(0, 4))
            if key == "method":
                widget = ttk.Combobox(form, textvariable=self.fields[key], values=METHODS, state="readonly", width=18)
            else:
                widget = ttk.Entry(form, textvariable=self.fields[key], width=18)
            widget.grid(row=row + 1, column=col, columnspan=span, sticky="ew", padx=(0, 10), pady=(0, 12))
            if key == "amount":
                self.amount_entry = widget
        self.print_after_save = tk.BooleanVar(value=self.ledger.preference("print_after_save") == "1")
        ttk.Checkbutton(form, text="Print after save", variable=self.print_after_save,
                        style="Card.TCheckbutton", command=self.save_print_preference).grid(row=8, column=0, sticky="w")
        ttk.Label(form, text="Opens print preview in your browser.", style="Card.TLabel").grid(
            row=8, column=1, columnspan=2, sticky="w")
        actions = ttk.Frame(self.new_tab)
        actions.grid(row=1, sticky="ew", pady=14)
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Save & print", command=lambda: self.save(force_print=True)).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(actions, text="Save receipt", style="Save.TButton", command=self.save).grid(row=0, column=2)
        self.pos_hint = tk.StringVar()
        ttk.Label(self.new_tab, textvariable=self.pos_hint, wraplength=1000).grid(row=2, sticky="w")
        self.fields["method"].trace_add("write", lambda *_: self.update_pos_hint())
        self.update_pos_hint()

    def build_history(self):
        page = self.history_tab
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)
        filters = ttk.Frame(page)
        filters.grid(row=0, sticky="ew", pady=(0, 10))
        self.date_from, self.date_to = tk.StringVar(), tk.StringVar()
        for col, (caption, variable) in enumerate((("From (YYYY-MM-DD)", self.date_from), ("To (YYYY-MM-DD)", self.date_to))):
            ttk.Label(filters, text=caption).grid(row=0, column=col * 2, padx=(0, 6))
            ttk.Entry(filters, textvariable=variable, width=13).grid(row=0, column=col * 2 + 1, padx=(0, 12))
        ttk.Button(filters, text="Apply", command=self.apply_filters).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(filters, text="All dates", command=self.clear_filters).grid(row=0, column=5)
        self.filtered_summary = tk.StringVar()
        ttk.Label(page, textvariable=self.filtered_summary).grid(row=1, sticky="w", pady=(0, 8))
        toolbar = ttk.Frame(page)
        toolbar.grid(row=2, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        self.all_checked = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Select all for print", variable=self.all_checked, command=self.toggle_all).grid(row=0, column=0)
        self.print_button = ttk.Button(toolbar, text="Print selected (0)", command=self.print_selected, state="disabled")
        self.print_button.grid(row=0, column=2, padx=(0, 6))
        ttk.Button(toolbar, text="Refresh", command=self.refresh).grid(row=0, column=3, padx=(0, 6))
        self.void_button = ttk.Button(toolbar, text="Void selected", command=self.void, state="disabled")
        self.void_button.grid(row=0, column=4, padx=(0, 6))
        ttk.Button(toolbar, text="Export CSV", command=self.export).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(toolbar, text="Back up data", command=self.backup).grid(row=0, column=6)
        table = ttk.Frame(page)
        table.grid(row=3, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        columns = ("print", "id", "date", "amount", "payer", "method", "reference", "card_brand", "card_last4", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse", height=8)
        for key, heading, width in zip(columns,
                                      ("Print", "Receipt", "Received date", f"Amount ({self.currency})", "Received from",
                                       "Method", "Invoice / reference", "Card type", "Card ending", "Status"),
                                      (50, 65, 100, 125, 155, 150, 150, 100, 100, 85)):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=45, stretch=key != "print",
                             anchor="center" if key == "print" else "e" if key == "amount" else "w")
        self.tree.grid(sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure("voided", foreground="#8b4a4a")
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Button-1>", self.toggle_print)
        self.tree.bind("<Double-1>", self.show_details)
        ttk.Label(page, text="Click [ ] in the Print column to choose receipts. Double-click a row for full details.").grid(
            row=4, sticky="w", pady=(8, 0))
        self.details = tk.StringVar(value="No receipts yet.")
        ttk.Label(page, textvariable=self.details, wraplength=1020).grid(row=5, sticky="w", pady=(6, 0))

    def refresh(self, filters=None):
        selected_filters = self.active_filters if filters is None else filters
        try:
            rows = self.ledger.receipts(*selected_filters)
            today, total, count = self.ledger.totals()
        except (ValueError, sqlite3.Error, OSError) as error:
            self.diagnostics.exception("ui_error", error)
            messagebox.showerror("Could not load receipts", str(error), parent=self.root)
            return False
        self.active_filters = selected_filters
        self.rows = {str(row["id"]): row for row in rows}
        self.checked_ids.intersection_update(self.rows)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for key, row in self.rows.items():
            self.tree.insert("", "end", iid=key,
                             values=("[x]" if key in self.checked_ids else "[ ]", row["id"], row["received_date"],
                                     format_amount(row["amount_minor"], self.currency), row["payer"], row["method"],
                                     row["reference"], row["card_brand"], "**** " + row["card_last4"] if row["card_last4"] else "",
                                     "Voided" if row["voided_at"] else "Recorded"),
                             tags=("voided",) if row["voided_at"] else ())
        self.today_total.set(f"{self.currency} {format_amount(today, self.currency)}")
        self.all_total.set(f"{self.currency} {format_amount(total, self.currency)}")
        self.count.set(str(count))
        filtered_total = sum(row["amount_minor"] for row in rows if not row["voided_at"])
        self.filtered_summary.set(f"Showing {len(rows)} receipts · Active total in this view: "
                                  f"{self.currency} {format_amount(filtered_total, self.currency)} · Voids excluded from total")
        self.void_button.state(["disabled"])
        self.details.set("Choose a receipt to view its details." if rows else "No receipts match this date range.")
        self.update_print_selection()
        return True

    def apply_filters(self):
        self.refresh(filters=(self.date_from.get().strip(), self.date_to.get().strip()))

    def clear_filters(self):
        self.date_from.set("")
        self.date_to.set("")
        self.refresh(filters=("", ""))

    def save_print_preference(self):
        try:
            self.ledger.set_preference("print_after_save", "1" if self.print_after_save.get() else "0")
        except (sqlite3.Error, OSError) as error:
            self.print_after_save.set(not self.print_after_save.get())
            messagebox.showerror("Preference not saved", str(error), parent=self.root)

    def save(self, force_print=False):
        if self.active_usb_probe:
            self.active_usb_probe.window.lift()
            return
        if self.active_pos:
            self.active_pos.window.lift()
            return
        entry = dict(received_date=self.fields["date"].get().strip(), amount=self.fields["amount"].get(),
                     payer=self.fields["payer"].get(), method=self.fields["method"].get(),
                     reference=self.fields["reference"].get(), notes=self.fields["notes"].get(),
                     payer_phone=self.fields["payer_phone"].get(), payer_address=self.fields["payer_address"].get())
        if entry["method"] == BANK_METHOD:
            self.begin_bank_payment(entry)
            return
        try:
            receipt_id = self.ledger.add(**entry)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.diagnostics.exception("receipt_save_error", error)
            messagebox.showerror("Receipt not saved", str(error), parent=self.root)
            return
        self.receipt_saved(receipt_id, print_now=force_print or self.print_after_save.get())

    def receipt_saved(self, receipt_id, print_now=False, clear_form=True):
        if clear_form:
            for key in ("amount", "payer", "payer_phone", "payer_address", "reference", "notes"):
                self.fields[key].set("")
            self.fields["date"].set(date.today().isoformat())
        self.refresh()
        if str(receipt_id) in self.rows:
            self.tree.selection_set(str(receipt_id))
            self.tree.see(str(receipt_id))
        self.status.set(f"Receipt #{receipt_id} saved successfully.")
        self.amount_entry.focus_set()
        if print_now:
            self.print_records([receipt_id], receipt_saved=True)

    def update_pos_hint(self):
        if self.fields["method"].get() == BANK_METHOD:
            self.pos_hint.set("Bank POS: a compatible supplier connector and checked USB/ECR connection are required. "
                              "A receipt is saved only after approval; print preview then opens automatically.")
        else:
            self.pos_hint.set("Manual recording. This payment method does not send a transaction to the bank POS.")

    def open_pos_setup(self):
        if self.pos_setup and self.pos_setup.window.winfo_exists():
            self.pos_setup.window.lift()
            self.pos_setup.refresh_pending()
            return
        self.pos_setup = PosSetup(self)

    def diagnostic_settings(self):
        try:
            if self.pos_setup and self.pos_setup.window.winfo_exists():
                return config_fields({key: variable.get() for key, variable in self.pos_setup.variables.items()})
            return config_fields(PosConfig.from_json(self.ledger.preference("pos_config")))
        except (ValueError, OSError, sqlite3.Error, tk.TclError):
            return {}

    def open_diagnostics(self):
        if self.diagnostics_window and self.diagnostics_window.window.winfo_exists():
            self.diagnostics_window.window.lift()
            self.diagnostics_window.refresh_log()
            return
        self.diagnostics_window = DiagnosticsWindow(self)

    def export_diagnostics(self, parent=None, port=None):
        destination = filedialog.asksaveasfilename(
            parent=parent or self.root, title="Export diagnostic log to share", defaultextension=".txt",
            initialfile=f"receipt-recorder-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt",
            filetypes=[("Diagnostic text log", "*.txt")])
        if not destination:
            return
        try:
            snapshot = self.diagnostic_settings()
            if port is not None:
                snapshot["port"] = port.strip().upper()
            ports = serial_ports()
            snapshot.update(ports=ports, port_count=len(ports))
            self.diagnostics.export(destination, snapshot=snapshot, protected=[self.ledger.path])
        except (ValueError, OSError) as error:
            self.diagnostics.exception("diagnostic_export_error", error, error_code="export_error")
            messagebox.showerror("Could not export log", "Choose a writable .txt file outside the application log folder. "
                                 "Your receipt database is not included.", parent=parent or self.root)
            return
        self.status.set(f"Diagnostic log exported to {destination}. You can attach this .txt file when asking for support.")
        messagebox.showinfo("Diagnostic log exported", "The diagnostic .txt file is saved. You can attach it in this chat.\n\n"
                            "It contains app/Windows details, COM settings and coded errors, without the receipt database, "
                            "customer details, card data or raw SDK messages.", parent=parent or self.root)

    def check_usb_port(self, port, parent=None, after_done=None):
        if self.active_pos or self.active_usb_probe:
            self.diagnostics.record("usb_probe_result", result_code="skipped_active", error_code="operation_active")
            messagebox.showinfo("Another check is running", "Wait for the current POS or USB check to finish.", parent=parent or self.root)
            return False
        try:
            allow_open = not bool(self.ledger.pending_pos())
        except sqlite3.Error as error:
            self.diagnostics.exception("usb_probe_error", error, error_code="database_error")
            allow_open = False
        self.diagnostics.record("usb_probe_start", port=port.strip().upper(), **{
            key: value for key, value in self.diagnostic_settings().items() if key != "port"})
        def done(result):
            self.active_usb_probe = None
            self.diagnostics.record("usb_probe_result", **result)
            self.status.set("USB / COM diagnostic check finished. Use Export diagnostic log to share its result.")
            if after_done:
                after_done(result)
            messagebox.showinfo("USB / COM diagnostic result", probe_message(result), parent=parent or self.root)
        self.active_usb_probe = UsbProbeWindow(parent or self.root, port, allow_open, done, self.diagnostics)
        return True

    def report_ui_error(self, kind, error, traceback):
        self.diagnostics.exception("ui_error", error)
        message = "An unexpected application error occurred. Open Diagnostics → Export diagnostic log to share the coded error."
        try:
            if self.active_pos or self.ledger.pending_pos():
                message += "\n\nA bank payment may still be unconfirmed. Check Pending payments before taking another card payment."
        except sqlite3.Error:
            pass
        messagebox.showerror("Application error", message, parent=self.root)

    def start_pos_operation(self, config, operation, attempt, done, parent=None):
        if self.active_usb_probe:
            self.active_usb_probe.window.lift()
            return False
        if self.active_pos:
            self.active_pos.window.lift()
            return False
        request_id = attempt["id"] if attempt is not None else new_request_id()
        amount = attempt["amount_minor"] if attempt is not None else 0
        currency = attempt["currency"] if attempt is not None else self.currency
        def completed(result, error, cancel_requested):
            self.active_pos = None
            done(result, error, cancel_requested)
        self.active_pos = PaymentProgress(parent or self.root, BankConnector(config, logger=self.diagnostics), operation,
                                          request_id, amount, currency, completed, logger=self.diagnostics)
        self.active_pos.start()
        return True

    def begin_bank_payment(self, entry):
        try:
            self.ledger.validate_entry(**entry)
            config = PosConfig.from_json(self.ledger.preference("pos_config"))
            config.validate()
            attempt = self.ledger.begin_pos_payment(entry, config.provider_id)
        except (ValueError, OSError, sqlite3.Error) as error:
            self.diagnostics.exception("pos_start_error", error, **self.diagnostic_settings())
            messagebox.showerror("Bank POS payment not started", str(error), parent=self.root)
            return
        self.status.set("Bank payment started. Waiting for a confirmed terminal result; no receipt saved yet.")
        def completed(result, error, cancelled):
            if result is None:
                result = PosResult(attempt["id"], "cancelled", attempt["provider_id"], attempt["amount_minor"], attempt["currency"])
            self.complete_bank_payment(result, error=error, cancelled=cancelled, new_entry=True)
        self.start_pos_operation(config, "purchase", attempt, completed)

    def complete_bank_payment(self, result, *, error="", cancelled=False, new_entry=False):
        try:
            receipt_id = self.ledger.finish_pos_payment(result)
        except (ValueError, OSError, sqlite3.Error) as error:
            self.diagnostics.exception("pos_record_error", error, attempt_tag=attempt_tag(result.request_id))
            self.status.set("Bank outcome needs attention. Do not charge again; check Pending payments in Bank POS setup.")
            messagebox.showerror("Payment needs reconciliation", "The payment result could not be recorded safely. "
                                 "Do not charge again. Check the terminal/provider and Pending payments.\n\n"
                                 f"Request ID: {result.request_id}", parent=self.root)
            return
        if receipt_id is not None:
            self.diagnostics.record("pos_recorded", attempt_tag=attempt_tag(result.request_id), status="approved")
            self.receipt_saved(receipt_id, print_now=True, clear_form=new_entry)
            if cancelled:
                messagebox.showwarning("Terminal approved payment", "The terminal approved before cancellation was confirmed. "
                                       "The approved receipt has been recorded. Any required refund must be handled with the provider.", parent=self.root)
        elif result.status in ("cancelled", "declined"):
            self.diagnostics.record("pos_result", attempt_tag=attempt_tag(result.request_id), status=result.status)
            self.status.set(f"Bank payment {result.status}. No receipt was saved.")
            messagebox.showinfo("No receipt saved", error or f"The terminal confirmed the payment was {result.status}.", parent=self.root)
        else:
            self.diagnostics.record("pos_result", attempt_tag=attempt_tag(result.request_id), status="unknown")
            self.status.set("Payment outcome is unconfirmed. No receipt saved. Do not charge again; check Pending payments.")
            messagebox.showwarning("Payment outcome unconfirmed", "No receipt has been saved. The terminal may still have processed the payment. "
                                   "Do not charge again. Open Bank POS setup → Pending payments and check its status.\n\n"
                                   f"Request ID: {result.request_id}", parent=self.root)
        if self.pos_setup and self.pos_setup.window.winfo_exists():
            self.pos_setup.refresh_pending()

    def check_pending_payment(self, request_id, parent=None, after_done=None):
        try:
            attempt = self.ledger.pos_attempt(request_id)
            config = PosConfig.from_json(self.ledger.preference("pos_config"))
            config.validate()
            if config.provider_id != attempt["provider_id"]:
                raise PosError("Restore the connector settings for this payment's original provider before checking its status.")
        except (ValueError, OSError, sqlite3.Error) as error:
            self.diagnostics.exception("pos_status_error", error)
            messagebox.showerror("Status check not started", str(error), parent=parent or self.root)
            return
        def done(result, error, _cancelled):
            if result is None:
                messagebox.showwarning("Status still unconfirmed", error, parent=self.root)
            else:
                self.complete_bank_payment(result, new_entry=False)
            if after_done:
                after_done()
        self.start_pos_operation(config, "status", attempt, done, parent=parent)

    def select(self, _event=None):
        selection = self.tree.selection()
        row = self.rows.get(selection[0]) if selection else None
        self.void_button.state(["!disabled"] if row and not row["voided_at"] else ["disabled"])
        if row:
            detail = f"Receipt #{row['id']} | From: {row['payer'] or 'Not entered'} | " \
                     f"Phone: {row['payer_phone'] or 'Not entered'} | Notes: {row['notes'] or 'None'}"
            self.details.set(textwrap.shorten(detail, width=210, placeholder="… (double-click for all details)"))

    def show_details(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        try:
            row = self.ledger.receipt(int(selection[0]))
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not open receipt", str(error), parent=self.root)
            return
        values = receipt_values(row, self.currency)
        text = "\n\n".join(f"{label}: {values[key]}" for key, label in FIELD_LABELS.items())
        window = tk.Toplevel(self.root)
        window.title(f"Receipt #{row['id']} — full details")
        window.geometry("650x600")
        widget = tk.Text(window, wrap="word", padx=16, pady=16, font="TkDefaultFont")
        widget.pack(fill="both", expand=True)
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        ttk.Button(window, text="Print this receipt", command=lambda: self.print_records([row["id"]], reprint=True)).pack(pady=10)

    def toggle_print(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return
        key = self.tree.identify_row(event.y)
        if key:
            if key in self.checked_ids:
                self.checked_ids.remove(key)
            else:
                self.checked_ids.add(key)
            self.tree.set(key, "print", "[x]" if key in self.checked_ids else "[ ]")
            self.tree.selection_set(key)
            self.update_print_selection()
            return "break"

    def toggle_all(self):
        self.checked_ids = set(self.rows) if self.all_checked.get() else set()
        for key in self.rows:
            self.tree.set(key, "print", "[x]" if key in self.checked_ids else "[ ]")
        self.update_print_selection()

    def update_print_selection(self):
        count = len(self.checked_ids)
        self.print_button.configure(text=f"Print selected ({count})")
        self.print_button.state(["!disabled"] if count else ["disabled"])
        self.all_checked.set(bool(self.rows) and count == len(self.rows))

    def print_selected(self):
        ids = [int(key) for key in self.rows if key in self.checked_ids]
        if not ids:
            messagebox.showinfo("Choose receipts", "Tick the Print boxes next to the receipts you want to print.", parent=self.root)
            return
        self.print_records(ids, reprint=True)

    def print_records(self, ids, reprint=False, receipt_saved=False):
        try:
            rows = [self.ledger.receipt(receipt_id) for receipt_id in ids]
            design = ReceiptDesign.from_json(self.ledger.preference("receipt_design"))
            open_receipts(rows, self.currency, design, self.ledger.path.parent / "printouts", reprint=reprint)
        except (ValueError, OSError, sqlite3.Error) as error:
            self.diagnostics.exception("print_error", error)
            prefix = "Your receipt is saved. Do not enter it again; you can reprint it from history.\n\n" if receipt_saved else ""
            messagebox.showwarning("Could not open print preview", prefix + str(error), parent=self.root)
            return False
        saved = f"Receipt #{ids[0]} saved. " if receipt_saved else ""
        self.status.set(saved + f"Print preview opened for {len(ids)} receipt(s). Choose your printer and finish printing in the browser.")
        return True

    def open_designer(self):
        if self.designer_window and self.designer_window.window.winfo_exists():
            self.designer_window.window.lift()
            return
        try:
            design = ReceiptDesign.from_json(self.ledger.preference("receipt_design"))
        except ValueError as error:
            messagebox.showwarning("Design needs attention", str(error), parent=self.root)
            design = ReceiptDesign()
        sample = dict(id="SAMPLE", received_date=date.today().isoformat(), amount_minor=12550,
                      payer="Example Customer / Company", payer_phone="050 000 0000", payer_address="Example street, Riyadh",
                      method=BANK_METHOD, reference="INV-001", notes="Example receipt for design preview only.",
                      card_brand="Visa", card_last4="1234", pos_provider_id="Example-provider", pos_transaction_id="EXAMPLE-001",
                      pos_authorization_code="123456", pos_terminal_id="EXAMPLE-TERMINAL",
                      created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), voided_at=None)
        def save_design(updated):
            self.ledger.set_preference("receipt_design", updated.to_json())
            self.status.set("Your 80 mm receipt design has been saved.")
        self.designer_window = ReceiptDesigner(self.root, design, sample, self.currency,
                                               self.ledger.path.parent / "printouts", save_design)

    def void(self):
        selection = self.tree.selection()
        if not selection:
            return
        receipt_id = int(selection[0])
        if not messagebox.askyesno("Void receipt", f"Void receipt #{receipt_id}?\n\n"
                                  "It will stay in history and print as VOIDED, but no longer count in totals.\n"
                                  "This does not cancel or refund a bank payment.", parent=self.root):
            return
        try:
            self.ledger.void(receipt_id)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.diagnostics.exception("receipt_void_error", error)
            messagebox.showerror("Could not void receipt", str(error), parent=self.root)
            return
        self.refresh()
        self.status.set(f"Receipt #{receipt_id} voided. Add a new receipt if you need to correct it.")

    def export(self):
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Export receipts in the current date range", defaultextension=".csv",
            initialfile=f"receipts-{date.today().isoformat()}.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = self.ledger.export_csv(Path(destination), *self.active_filters)
        except (ValueError, sqlite3.Error, OSError) as error:
            self.diagnostics.exception("csv_error", error)
            messagebox.showerror("Export failed", str(error), parent=self.root)
            return
        self.status.set(f"Exported {count} receipts from this view (including voids) to {destination}")

    def backup(self):
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Back up receipt data and design", defaultextension=".sqlite3",
            initialfile=f"receipts-backup-{date.today().isoformat()}.sqlite3",
            filetypes=[("Receipt database", "*.sqlite3")])
        if not destination:
            return
        try:
            self.ledger.backup(Path(destination))
        except (ValueError, sqlite3.Error, OSError) as error:
            self.diagnostics.exception("backup_error", error)
            messagebox.showerror("Backup failed", str(error), parent=self.root)
            return
        self.status.set(f"Backup saved to {destination}")

    def close(self):
        if self.active_pos:
            self.active_pos.request_cancel()
            self.active_pos.window.lift()
            self.status.set("Waiting for the POS operation to finish. Do not disconnect the terminal during a payment.")
            return
        if self.active_usb_probe:
            self.active_usb_probe.spinner.stop()
            self.active_usb_probe.window.destroy()
            self.active_usb_probe = None
        self.diagnostics.record("app_close")
        self.ledger.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Record and print receipts locally on this computer.")
    parser.add_argument("--data-dir", type=Path, help="Use a separate folder for the receipt database.")
    args = parser.parse_args()
    root = tk.Tk()
    root.withdraw()
    path = args.data_dir / "receipts.sqlite3" if args.data_dir else default_data_path()
    diagnostics = DiagnosticLog(path.parent / "logs", APP_VERSION)
    diagnostics.record("app_start")
    ledger = None
    try:
        ledger = Ledger(path)
        if ledger.currency is None:
            ledger.set_currency("SAR")
        ReceiptApp(root, ledger, diagnostics=diagnostics)
    except (ValueError, sqlite3.Error, OSError) as error:
        diagnostics.exception("startup_error", error)
        messagebox.showerror("Could not open Receipt Recorder", f"{error}\n\nData location: {path}", parent=root)
        if ledger:
            ledger.close()
        root.destroy()
        return 1
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
