import csv
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import ReceiptApp
from ledger import Ledger
from receipt_printing import ReceiptDesign


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "Requires a desktop or Xvfb")
class DesktopSmokeTest(unittest.TestCase):
    def test_save_restart_export_backup_and_void(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipts.sqlite3"
            ledger = Ledger(path)
            ledger.set_currency("SAR")
            root = tk.Tk()
            app = ReceiptApp(root, ledger)
            try:
                root.update()
                app.fields["amount"].set("125.50")
                app.fields["payer"].set("Customer A")
                app.fields["reference"].set("POS-123")
                app.fields["notes"].set("First receipt")
                app.save()
                root.update()
                self.assertEqual(app.today_total.get(), "SAR 125.50")
                self.assertEqual(app.count.get(), "1")
                self.assertEqual(len(app.tree.get_children()), 1)
                self.assertEqual(app.fields["amount"].get(), "")
                with patch("app.messagebox.showerror") as error_dialog:
                    app.fields["amount"].set("-5")
                    app.save()
                    error_dialog.assert_called_once()
                self.assertEqual(len(ledger.receipts()), 1)
            finally:
                app.close()

            ledger = Ledger(path)
            root = tk.Tk()
            app = ReceiptApp(root, ledger)
            try:
                root.update()
                self.assertEqual(app.all_total.get(), "SAR 125.50")
                app.tree.selection_set("1")
                app.select()
                self.assertIn("First receipt", app.details.get())
                export = Path(folder) / "receipts.csv"
                with patch("app.filedialog.asksaveasfilename", return_value=str(export)):
                    app.export()
                with export.open(encoding="utf-8-sig", newline="") as source:
                    self.assertEqual(list(csv.DictReader(source))[0]["Amount"], "125.50")
                backup = Path(folder) / "backup.sqlite3"
                with patch("app.filedialog.asksaveasfilename", return_value=str(backup)):
                    app.backup()
                restored = Ledger(backup)
                try:
                    self.assertEqual(restored.totals()[1], 12550)
                finally:
                    restored.close()
                with patch("app.messagebox.askyesno", return_value=False):
                    app.void()
                self.assertEqual(app.count.get(), "1")
                with patch("app.messagebox.askyesno", return_value=True):
                    app.void()
                root.update()
                self.assertEqual(app.all_total.get(), "SAR 0.00")
                self.assertEqual(app.count.get(), "0")
                self.assertEqual(app.tree.item("1", "values")[-1], "Voided")
            finally:
                app.close()

    def test_print_after_save_persists_and_print_failure_does_not_duplicate(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "receipts.sqlite3")
            ledger.set_currency("SAR")
            root = tk.Tk()
            app = ReceiptApp(root, ledger)
            try:
                app.print_after_save.set(True)
                app.save_print_preference()
                app.fields["amount"].set("100")
                app.fields["payer"].set("Customer A")
                app.fields["payer_phone"].set("0501234567")
                app.fields["payer_address"].set("Riyadh")
                with patch("app.open_receipts") as printing:
                    app.save()
                self.assertEqual(ledger.preference("print_after_save"), "1")
                self.assertEqual(len(ledger.receipts()), 1)
                printed_rows = printing.call_args.args[0]
                self.assertEqual(printed_rows[0]["payer_phone"], "0501234567")
                self.assertEqual(printed_rows[0]["payer_address"], "Riyadh")
                self.assertEqual(app.fields["payer_phone"].get(), "")
                app.fields["amount"].set("50")
                with patch("app.open_receipts", side_effect=OSError("No browser")), patch("app.messagebox.showwarning") as warning:
                    app.save()
                warning.assert_called_once()
                self.assertIn("saved", warning.call_args.args[1])
                self.assertEqual(len(ledger.receipts()), 2)
                self.assertEqual(ledger.totals()[1], 15000)
            finally:
                app.close()

    def test_filters_checkboxes_and_reprint_only_selected_records(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "receipts.sqlite3")
            ledger.set_currency("SAR")
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            first = ledger.add(yesterday, "10", reference="INV-1")
            ledger.add(date.today().isoformat(), "20", reference="INV-2")
            root = tk.Tk()
            app = ReceiptApp(root, ledger)
            try:
                app.notebook.select(app.history_tab)
                root.update()
                app.date_from.set(yesterday)
                app.date_to.set(yesterday)
                app.apply_filters()
                root.update()
                self.assertEqual(list(app.rows), [str(first)])
                x, y, width, height = app.tree.bbox(str(first), "print")
                app.tree.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
                root.update()
                self.assertEqual(app.checked_ids, {str(first)})
                with patch("app.open_receipts") as printing:
                    app.print_selected()
                self.assertEqual([row["id"] for row in printing.call_args.args[0]], [first])
                self.assertTrue(printing.call_args.kwargs["reprint"])
                app.fields["amount"].set("5")
                app.save()  # A new receipt outside the visible range must still save successfully.
                self.assertEqual(len(ledger.receipts()), 3)
                self.assertEqual(list(app.rows), [str(first)])
                app.date_from.set("invalid")
                with patch("app.messagebox.showerror") as error:
                    app.apply_filters()
                error.assert_called_once()
                self.assertEqual(app.active_filters, (yesterday, yesterday))
                app.clear_filters()
                self.assertEqual(len(app.rows), 3)
                app.all_checked.set(True)
                app.toggle_all()
                self.assertEqual(len(app.checked_ids), 3)
                app.all_checked.set(False)
                app.toggle_all()
                self.assertEqual(app.checked_ids, set())
            finally:
                app.close()

    def test_designer_updates_preview_saves_layout_and_opens_exact_preview(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "receipts.sqlite3")
            ledger.set_currency("SAR")
            root = tk.Tk()
            app = ReceiptApp(root, ledger)
            try:
                app.open_designer()
                designer = app.designer_window
                root.update()
                designer.variables["merchant_name"].set("My Shop")
                designer.variables["font_size"].set("11")
                designer.variables["margin_mm"].set("4")
                designer.field_list.selection_clear(0, "end")
                designer.field_list.selection_set(0)
                designer.choose_field()
                designer.field_label.set("Voucher number")
                designer.move(1)
                root.update()
                self.assertIn("My Shop", designer.preview.get("1.0", "end"))
                self.assertIn("Voucher number", designer.preview.get("1.0", "end"))
                with patch("receipt_designer.open_receipts") as preview:
                    designer.browser_preview()
                self.assertFalse(preview.call_args.kwargs["auto_print"])
                designer.save()
                saved = ReceiptDesign.from_json(ledger.preference("receipt_design"))
                self.assertEqual(saved.merchant_name, "My Shop")
                self.assertEqual(saved.margin_mm, 4)
                self.assertEqual(saved.labels["id"], "Voucher number")
                self.assertEqual(saved.field_order[1], "id")
                self.assertEqual(len(ledger.receipts()), 0)
            finally:
                app.close()



if __name__ == "__main__":
    unittest.main()
