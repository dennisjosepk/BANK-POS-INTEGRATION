# Receipt Recorder

A small Windows desktop application for recording money received in **Saudi riyals (SAR)**.
It works offline and saves receipts on the computer. This first version records receipts manually;
it does not send transactions to a bank POS machine.

![Application design preview with example receipts](docs/design-preview.png)

The preview contains example data only. The application starts with an empty receipt book.

## Run on your Windows computer

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/windows/).
   Keep Tcl/Tk enabled and select **Add Python to PATH** during installation.
2. Download this project and extract the ZIP to a folder.
3. Double-click **start_windows.bat** in that folder.

No extra Python packages are needed to run the application. Windows 10 or 11 is recommended.
The source supports Python 3.10 and later; the development and packaging target is Python 3.12.

## Record a receipt

Enter the date, amount (for example `125.50`), and payment method, then click **Save receipt**.
You may also enter the payer, bank/POS reference, and notes. Do not put card numbers or PINs in these fields.

- The summary shows today's receipts, all-time receipts, and the number of active receipts.
- **Card (manual entry)** records an amount you have already confirmed on the separate POS machine.
  It does not charge a card or verify that a bank payment succeeded.
- Select a receipt to see its notes.
- To correct a mistake, use **Void selected**, then add a replacement receipt.
  The original stays visible and is excluded from totals. Voiding does not issue a bank refund.
- **Export CSV** saves all receipts, including voided ones, for Excel. Use the Status column to exclude voids.
- **Back up data** saves a complete copy of the receipt database. Keep backups in another location.

Amounts are stored as whole halalas (100 halalas = 1 SAR), avoiding floating-point rounding errors.
The date uses the computer's local calendar date. Audit timestamps are stored in UTC.

## Where the data is saved

On Windows:

```text
%LOCALAPPDATA%\ReceiptRecorder\receipts.sqlite3
```

Paste `%LOCALAPPDATA%\ReceiptRecorder` into File Explorer's address bar to open this folder.
Closing, upgrading, or moving the application does not erase this data.
This is a local receipt book for one Windows user, without sign-in, encryption, or bank reconciliation.

To restore a backup: close all copies of the app, keep a copy of the current database,
then copy your backup into this folder and name it `receipts.sqlite3`.
Reopen the application and verify the receipts. CSV exports are reports, not database backups.

To use a separate data folder:

```powershell
py -3 app.py --data-dir "C:\MyReceiptBook"
```

## Build an EXE

On Windows with Python installed, double-click **build_windows.bat**.
It creates `dist\ReceiptRecorder.exe`. The resulting EXE runs without a separate Python installation.
Build it on Windows; PyInstaller cannot build a Windows EXE from Linux.
The EXE is unsigned, so Windows may show an unknown-publisher notice.

Alternatively, after this project has been pushed to GitHub, open **Actions → Build Windows application → Run workflow**.
Download the **ReceiptRecorder-Windows** artifact from a successful run and extract the EXE.
Build results and downloadable files appear in the Actions run.

## Future bank/POS connections

The supplied `ViknPOSIntegration.exe` is a compiled .NET 8 Windows application. Static inspection
found COM-port, baud-rate, purchase-amount and payment-result components, along with Mada SDK components.
It was inspected as a reference, not executed or copied into this application.

The receipt-recording logic is separate from the screen so a supported POS connector can later save
confirmed payments. Different banks or machines may need different SDKs, protocols, drivers and ECR settings.
A universal connector cannot be assumed from the brand or the COM port alone.

For each intended integration, obtain the provider's ECR/POS integration documentation and SDK,
supported terminal models, connection type (USB/serial/LAN), and test access. A connector should
only record a payment once after an approved response; declines and timeouts must not be treated
as receipts. Test timeout/recovery and duplicate prevention before using real payments.

## Development

Use the existing checkout in the isolated cloud task; a Git worktree is unnecessary.

```bash
cd /workspace/BANK-POS-INTEGRATION
python3 -m unittest discover -s tests -v
python3 app.py --data-dir /tmp/receipt-recorder-demo
```

Tkinter needs a graphical display. In a headless Linux environment, run the GUI tests with Xvfb;
the ledger tests also run without a display. The desktop smoke test uses a temporary database,
creates a receipt through the UI, checks totals/history, restarts the UI against the same database,
then verifies export, backup, and void behavior. It does not charge a card or contact a bank.

Do not commit receipt databases, private payment details, generated EXEs or the supplied reference binary.
