"""Receipt Recorder: a small, offline Windows desktop application."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from ledger import METHODS, Ledger, format_amount


def default_data_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ReceiptRecorder" / "receipts.sqlite3"


class ReceiptApp:
    def __init__(self, root: tk.Tk, ledger: Ledger):
        self.root = root
        self.ledger = ledger
        self.currency = ledger.currency
        if self.currency is None:
            raise ValueError("Choose a ledger currency before opening the receipt screen.")
        self.rows = {}
        root.title("Receipt Recorder")
        root.geometry("1120x760")
        root.minsize(900, 680)
        root.configure(background="#f2f5fa")
        root.protocol("WM_DELETE_WINDOW", self.close)
        font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name, root).configure(family=font_family, size=10)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f5fa")
        style.configure("TLabel", background="#f2f5fa", foreground="#24334b", font=(font_family, 10))
        style.configure("Title.TLabel", font=(font_family, 23, "bold"))
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("Summary.TLabel", background="white", font=(font_family, 17, "bold"), foreground="#165c49")
        style.configure("TLabelframe", background="white", bordercolor="#dce3ed", relief="solid")
        style.configure("TLabelframe.Label", background="white", font=(font_family, 11, "bold"))
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=5)
        style.configure("TButton", padding=(12, 7), font=(font_family, 10))
        style.configure("Save.TButton", foreground="white", background="#165c49")
        style.map("Save.TButton", background=[("active", "#104737")])
        style.configure("Treeview", rowheight=29, font=(font_family, 10), borderwidth=0)
        style.configure("Treeview.Heading", font=(font_family, 10, "bold"), background="#e8eef5", padding=(5, 8))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body = ttk.Frame(root, padding=24)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        ttk.Label(body, text="Receipt Recorder", style="Title.TLabel").grid(sticky="w")
        ttk.Label(body, text="Record money received in Saudi riyals. Keep a clear daily record.").grid(sticky="w", pady=(2, 16))

        summary = ttk.Frame(body)
        summary.grid(row=2, sticky="ew", pady=(0, 18))
        self.today_total = tk.StringVar()
        self.all_total = tk.StringVar()
        self.count = tk.StringVar()
        for col, (caption, variable) in enumerate((("RECEIVED TODAY", self.today_total),
                                                  ("TOTAL RECEIVED", self.all_total),
                                                  ("ACTIVE RECEIPTS", self.count))):
            summary.columnconfigure(col, weight=1)
            panel = ttk.Frame(summary, style="Card.TFrame", padding=14)
            panel.grid(row=0, column=col, sticky="ew", padx=(0, 12 if col < 2 else 0))
            ttk.Label(panel, text=caption, style="Card.TLabel").pack(anchor="w")
            ttk.Label(panel, textvariable=variable, style="Summary.TLabel").pack(anchor="w", pady=(5, 0))

        form = ttk.LabelFrame(body, text="New receipt", padding=14)
        form.grid(row=3, sticky="ew")
        for col in range(4):
            form.columnconfigure(col, weight=1)
        self.fields = {key: tk.StringVar(value=value) for key, value in (
            ("date", date.today().isoformat()), ("amount", ""), ("payer", ""),
            ("method", METHODS[0]), ("reference", ""), ("notes", ""))}
        specifications = (("Received date (YYYY-MM-DD)", "date", 0, 0),
                          (f"Amount ({self.currency}) *", "amount", 0, 1),
                          ("Received from (optional)", "payer", 0, 2),
                          ("Payment method *", "method", 0, 3),
                          ("Reference (optional)", "reference", 2, 0),
                          ("Notes (optional)", "notes", 2, 1))
        for caption, key, row, col in specifications:
            span = 3 if key == "notes" else 1
            ttk.Label(form, text=caption, style="Card.TLabel").grid(row=row, column=col, columnspan=span,
                                              sticky="w", padx=(0, 12), pady=(0, 4))
            if key == "method":
                widget = ttk.Combobox(form, textvariable=self.fields[key], values=METHODS,
                                      state="readonly", width=20)
            else:
                widget = ttk.Entry(form, textvariable=self.fields[key], width=20)
            widget.grid(row=row + 1, column=col, columnspan=span, sticky="ew",
                        padx=(0, 12), pady=(0, 12))
            if key == "amount":
                self.amount_entry = widget
        ttk.Button(form, text="Save receipt", style="Save.TButton", command=self.save).grid(
            row=4, column=3, sticky="e", padx=(0, 12))
        ttk.Label(form, text="Manual recording. The POS machine is not connected.", style="Card.TLabel").grid(
            row=4, column=0, columnspan=3, sticky="w")

        toolbar = ttk.Frame(body)
        toolbar.grid(row=4, sticky="ew", pady=(18, 8))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(toolbar, text="Receipt history", font=(font_family, 12, "bold")).grid(sticky="w")
        ttk.Button(toolbar, text="Refresh", command=self.refresh).grid(row=0, column=1, padx=(0, 6))
        self.void_button = ttk.Button(toolbar, text="Void selected", command=self.void, state="disabled")
        self.void_button.grid(row=0, column=2, padx=(0, 6))
        ttk.Button(toolbar, text="Export CSV", command=self.export).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(toolbar, text="Back up data", command=self.backup).grid(row=0, column=4)

        table = ttk.Frame(body)
        table.grid(row=5, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        columns = ("id", "date", "amount", "payer", "method", "reference", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse", height=9)
        for key, heading, width in zip(columns,
                                      ("Receipt", "Received date", f"Amount ({self.currency})", "Received from",
                                       "Method", "Reference", "Status"),
                                      (65, 100, 130, 160, 150, 150, 85)):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=60, anchor="e" if key == "amount" else "w")
        self.tree.grid(sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure("voided", foreground="#8b4a4a")
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.details = tk.StringVar(value="No receipts yet. Add your first receipt above.")
        ttk.Label(body, textvariable=self.details, wraplength=1000).grid(row=6, sticky="w", pady=(8, 0))
        self.status = tk.StringVar(value="Ready. Receipts are saved automatically on this computer.")
        ttk.Label(body, textvariable=self.status).grid(row=7, sticky="w", pady=(10, 0))
        ttk.Label(body, text=f"Data: {ledger.path}", foreground="#65738a", wraplength=1000).grid(
            row=8, sticky="w", pady=(4, 0))
        self.refresh()
        self.amount_entry.focus_set()

    def refresh(self):
        try:
            rows = self.ledger.receipts()
            today, total, count = self.ledger.totals()
        except (sqlite3.Error, OSError) as error:
            messagebox.showerror("Could not load receipts", str(error), parent=self.root)
            return
        self.rows = {str(row["id"]): row for row in rows}
        for item in self.tree.get_children():
            self.tree.delete(item)
        for key, row in self.rows.items():
            self.tree.insert("", "end", iid=key,
                             values=(row["id"], row["received_date"], format_amount(row["amount_minor"], self.currency),
                                     row["payer"], row["method"], row["reference"],
                                     "Voided" if row["voided_at"] else "Recorded"),
                             tags=("voided",) if row["voided_at"] else ())
        self.today_total.set(f"{self.currency} {format_amount(today, self.currency)}")
        self.all_total.set(f"{self.currency} {format_amount(total, self.currency)}")
        self.count.set(str(count))
        self.void_button.state(["disabled"])
        self.details.set("Select a receipt to view its notes. Voided receipts are excluded from totals."
                         if rows else "No receipts yet. Add your first receipt above.")

    def save(self):
        try:
            receipt_id = self.ledger.add(
                self.fields["date"].get().strip(), self.fields["amount"].get(), self.fields["payer"].get(),
                self.fields["method"].get(), self.fields["reference"].get(), self.fields["notes"].get())
        except (ValueError, sqlite3.Error, OSError) as error:
            messagebox.showerror("Receipt not saved", str(error), parent=self.root)
            return
        for key in ("amount", "payer", "reference", "notes"):
            self.fields[key].set("")
        self.fields["date"].set(date.today().isoformat())
        self.refresh()
        self.tree.selection_set(str(receipt_id))
        self.tree.see(str(receipt_id))
        self.status.set(f"Receipt #{receipt_id} saved successfully.")
        self.amount_entry.focus_set()

    def select(self, _event=None):
        selection = self.tree.selection()
        row = self.rows.get(selection[0]) if selection else None
        self.void_button.state(["!disabled"] if row and not row["voided_at"] else ["disabled"])
        if row:
            self.details.set(f"Receipt #{row['id']} | Notes: {row['notes'] or 'None'}")

    def void(self):
        selection = self.tree.selection()
        if not selection:
            return
        receipt_id = int(selection[0])
        if not messagebox.askyesno("Void receipt", f"Void receipt #{receipt_id}?\n\n"
                                  "It will stay in the history and CSV, but will no longer count in totals.\n"
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
            parent=self.root, title="Export all receipts", defaultextension=".csv",
            initialfile=f"receipts-{date.today().isoformat()}.csv", filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        try:
            count = self.ledger.export_csv(Path(destination))
        except (ValueError, sqlite3.Error, OSError) as error:
            messagebox.showerror("Export failed", str(error), parent=self.root)
            return
        self.status.set(f"Exported {count} receipts (including voided entries) to {destination}")

    def backup(self):
        destination = filedialog.asksaveasfilename(
            parent=self.root, title="Back up receipt data", defaultextension=".sqlite3",
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
    parser = argparse.ArgumentParser(description="Record receipts locally on this computer.")
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
