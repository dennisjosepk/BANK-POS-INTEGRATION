import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import ReceiptApp
from ledger import Ledger


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


if __name__ == "__main__":
    unittest.main()
