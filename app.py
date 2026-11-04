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

APP_VERSION = "1.1.0"


def default_data_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ReceiptRecorder" / "receipts.sqlite3"


class ReceiptApp:
    def __init__(self, root: tk.Tk, ledger: Ledger):
        self.root, self.ledger = root, ledger
        self.currency = ledger.currency
        if self.currency is None:
            raise ValueError("Choose a ledger currency before opening the receipt screen.")
        self.rows = {}
        self.checked_ids = set()
        self.active_filters = ("", "")
        self.designer_window = None
        root.title(f"Receipt Recorder {APP_VERSION}")
        root.geometry("1140x800")
        root.minsize(1000, 720)
        root.configure(background="#f2f5fa")
        root.protocol("WM_DELETE_WINDOW", self.close)
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
        ttk.Button(header, text="Receipt designer · 80 mm", command=self.open_designer).grid(row=0, column=1)
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
        ttk.Label(self.new_tab, text="Manual recording. The bank POS machine is not connected.").grid(row=2, sticky="w")

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
        columns = ("print", "id", "date", "amount", "payer", "method", "reference", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse", height=8)
        for key, heading, width in zip(columns,
                                      ("Print", "Receipt", "Received date", f"Amount ({self.currency})", "Received from",
                                       "Method", "Invoice / reference", "Status"),
                                      (50, 65, 100, 125, 155, 150, 150, 85)):
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
                                     row["reference"], "Voided" if row["voided_at"] else "Recorded"),
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
        try:
            receipt_id = self.ledger.add(
                self.fields["date"].get().strip(), self.fields["amount"].get(), self.fields["payer"].get(),
                self.fields["method"].get(), self.fields["reference"].get(), self.fields["notes"].get(),
                self.fields["payer_phone"].get(), self.fields["payer_address"].get())
        except (ValueError, sqlite3.Error, OSError) as error:
            messagebox.showerror("Receipt not saved", str(error), parent=self.root)
            return
        for key in ("amount", "payer", "payer_phone", "payer_address", "reference", "notes"):
            self.fields[key].set("")
        self.fields["date"].set(date.today().isoformat())
        self.refresh()
        if str(receipt_id) in self.rows:
            self.tree.selection_set(str(receipt_id))
            self.tree.see(str(receipt_id))
        self.status.set(f"Receipt #{receipt_id} saved successfully.")
        self.amount_entry.focus_set()
        if force_print or self.print_after_save.get():
            self.print_records([receipt_id], receipt_saved=True)

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
                      method=METHODS[1], reference="INV-001", notes="Example receipt for design preview only.",
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
            messagebox.showerror("Backup failed", str(error), parent=self.root)
            return
        self.status.set(f"Backup saved to {destination}")

    def close(self):
        self.ledger.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Record and print receipts locally on this computer.")
    parser.add_argument("--data-dir", type=Path, help="Use a separate folder for the receipt database.")
    args = parser.parse_args()
    root = tk.Tk()
    root.withdraw()
    path = args.data_dir / "receipts.sqlite3" if args.data_dir else default_data_path()
    ledger = None
    try:
        ledger = Ledger(path)
        if ledger.currency is None:
            ledger.set_currency("SAR")
        ReceiptApp(root, ledger)
    except (ValueError, sqlite3.Error, OSError) as error:
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
