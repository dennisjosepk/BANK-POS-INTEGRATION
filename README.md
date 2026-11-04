# Receipt Recorder

A small Windows desktop application for recording money received in **Saudi riyals (SAR)**.
It works offline and saves receipts on the computer. This first version records receipts manually;
it does not send transactions to a bank POS machine.

Version **1.1.0** adds 80 mm thermal receipts, a receipt designer, customer contact details,
date filters and selected-record reprinting.

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
You may also enter the received-from name/company, phone, address, invoice/reference, and notes.
Do not put card numbers or PINs in these fields.

- The summary shows today's receipts, all-time receipts, and the number of active receipts.
- **Card (manual entry)** records an amount you have already confirmed on the separate POS machine.
  It does not charge a card or verify that a bank payment succeeded.
- Open **Receipt history & reprint** to see records. Double-click a row for all details.
- Enter **From** and **To** dates, then click **Apply**. Both boundary dates are included;
  either date can be blank. **All dates** clears the filter. The top summary always covers
  all dates; a separate summary shows the total for the filtered history.
- To correct a mistake, use **Void selected**, then add a replacement receipt.
  The original stays visible and is excluded from totals. Voiding does not issue a bank refund.
- **Export CSV** saves the filtered history, including customer contact details and voided entries,
  for Excel. Use the Status column to exclude voids.
- **Back up data** saves the receipt database, design, embedded logo and print preference.
  Keep backups in another location.

## Print with the Propos PP9000EU (80 mm)

1. Install the **Windows printer driver supplied for your Propos PP9000EU** and confirm that
   a Windows test page prints. The application uses this installed driver, rather than a raw USB protocol.
2. Set the driver's paper to **80 mm receipt/roll paper**.
3. In the app, use **Save & print**, or tick **Print after save** and click **Save receipt**.
4. A local receipt preview opens in your default browser and requests its print dialog.
   Choose the **PP9000EU**, 80 mm paper, **100% scale**, **Margins: None**, and turn browser
   **headers and footers off**. The receipt layout supplies its own margins.
5. Review the preview, then click **Print** in that dialog. If the dialog does not open automatically,
   use the **Print receipts** button at the top of the preview.

The checkbox is remembered after restarting. Printing always uses the saved record, including every
entered detail, receipt ID, amount/currency, received date, status and audit timestamps. If preview
cannot open, the receipt remains saved: reprint it from history instead of entering it again.
Cancelling a print dialog does not cancel or remove the receipt. Opening a preview is not confirmation
that the printer physically printed it.

### Reprint selected receipts

Open **Receipt history & reprint**, optionally filter the dates, and click the **[ ]** boxes in
the **Print** column for the records you want. **Select all for print** selects the visible rows.
Click **Print selected (N)**. Each selected receipt gets its own 80 mm page and a **REPRINT / COPY** label.
Voided receipts are also clearly marked **VOIDED**. Hidden records are not included.
You can also double-click a record and use **Print this receipt** in its details window.

![History and print selection](docs/history-preview.png)

### Design the receipt

Click **Receipt designer · 80 mm**. Customize the business name, address, phone, title, footer,
logo (PNG/JPEG/GIF up to 2 MB), font, font size, margins, header/footer alignment and separators.
The **Field order & labels** tab lets you reorder fields and rename their labels. All receipt
fields stay included. Paper width stays at 80 mm; receipt length adjusts to the content.

The side preview updates as you edit. Use **Open print preview** for the actual browser layout
and logo, then **Save design** to apply it to future prints and reprints. The default 4 mm margins
leave 72 mm for content; adjust them if your driver's printable area differs.
The designer uses an example record and does not create a receipt in your history.

![Receipt designer](docs/receipt-designer-preview.png)

Print previews are generated locally in the data folder's `printouts` directory. The application
does not upload receipts to a website. Generated previews older than seven days are removed when
the next preview is created; the underlying receipt records remain in the database.

The browser layout has been checked as 80 mm pages, including long notes and multiple receipts.
A physical PP9000EU test is still required on your Windows computer.

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

### Upgrade from version 1.0

Use **Back up data** in the old app, then close it. Download the new Windows build, extract it,
and run its `ReceiptRecorder.exe` as the same Windows user. It finds your existing data automatically.
On first launch it adds the phone/address fields without changing receipt numbers or amounts.
Before the upgrade it also creates `receipts-before-printing-update.sqlite3` beside the live database.

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

The suite also exercises database upgrades, customer details, print failures after saving,
date-range filtering, selected reprints and the designer. For a real browser check:

```bash
python3 -m pip install -r requirements-test.txt
python3 scripts/check_print_layout.py
```

This optional check uses installed Microsoft Edge on Windows or Chromium on Linux. It tests
the receipt's print button without sending a printer job, renders a PDF, and verifies the 80 mm
width, separate pages and complete long notes. It also runs in the Windows build workflow.

Do not commit receipt databases, private payment details, generated EXEs or the supplied reference binary.
