"""Check real browser page sizing and full content without sending a print job."""

from datetime import date
from pathlib import Path
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from receipt_printing import ReceiptDesign, render_receipts_html
from pos import BANK_METHOD


def main():
    row = dict(id=1, received_date=date.today().isoformat(), amount_minor=12550, payer="أحمد & Company",
               payer_phone="0501234567", payer_address="Riyadh, Example Street", method="Cash", reference="INV-001",
               notes="Payment received in full.", created_at="2026-11-04T08:00:00+00:00", voided_at=None)
    long_row = dict(row, id=2, payer_address=("Long address " * 35)[:490],
                    notes=("Detailed receipt note " * 45)[:950] + " END-OF-NOTES")
    bank_row = dict(row, id=3, method=BANK_METHOD, card_brand="Visa", card_last4="0012",
                    pos_transaction_id="BANK-TX-001", pos_authorization_code="AUTH1",
                    pos_terminal_id="TERM1", pos_provider_id="Example-provider")
    html = render_receipts_html([row, long_row, bank_row], "SAR", ReceiptDesign(merchant_name="Example Business"), reprint=True)
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as playwright:
        if sys.platform == "win32":
            browser = playwright.chromium.launch(channel="msedge", headless=True)
        else:
            executable = shutil.which("chromium") or shutil.which("google-chrome")
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_content(html, wait_until="load")
            page.evaluate("preparePages()")
            if errors:
                raise AssertionError(f"Receipt script errors: {errors}")
            # Test the actual print button, intercepting window.print rather than printing on a runner.
            page.evaluate("() => { window.__printCount = 0; window.print = () => { window.__printCount++; }; }")
            page.locator("#print-button").click()
            if page.evaluate("window.__printCount") != 1:
                raise AssertionError("The print button did not request printing exactly once")
            pdf = Path(folder) / "receipts.pdf"
            page.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True)
            reader = PdfReader(pdf)
            if len(reader.pages) != 3:
                raise AssertionError(f"Expected one page per receipt, got {len(reader.pages)}")
            for index, sheet in enumerate(reader.pages, 1):
                width = float(sheet.mediabox.width) * 25.4 / 72
                height = float(sheet.mediabox.height) * 25.4 / 72
                if abs(width - 80) >= 0.5:
                    raise AssertionError(f"Page {index} is {width} mm wide, expected 80 mm")
                text = sheet.extract_text()
                for expected in ("INV-001", "0501234567", "125.50", "REPRINT / COPY"):
                    if expected not in text:
                        raise AssertionError(f"Page {index} is missing {expected}")
                print(f"Page {index}: {width:.2f} mm wide, {height:.2f} mm high; receipt details present.")
                if "Thankyouforyourpayment." not in "".join(text.split()):
                    raise AssertionError(f"Page {index} footer was cut off")
            text = "".join(reader.pages[1].extract_text().split())
            if "".join(long_row["notes"].split()) not in text:
                raise AssertionError("Long notes were cut off")
            if "".join(long_row["payer_address"].split()) not in text:
                raise AssertionError("Long address was cut off")
            card_text = reader.pages[2].extract_text()
            for expected in ("Visa", "0012", "BANK-TX-001", "AUTH1", "TERM1"):
                if expected not in card_text:
                    raise AssertionError(f"Card receipt is missing {expected}")
            reader.close()
            print("Print button, 80 mm paper, separate receipts and long notes passed.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
