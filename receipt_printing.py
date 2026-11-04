"""80 mm receipts shared by the designer, original prints and history reprints.

Uses the user's browser print dialog and installed Windows printer drivers.
No printer-specific raw commands, network requests or bank operations are used.
"""

from __future__ import annotations

import base64
import json
import math
import secrets
import time
import webbrowser
from dataclasses import asdict, dataclass, field, fields
from html import escape
from pathlib import Path
from uuid import uuid4

from ledger import format_amount
from pos import BANK_METHOD


FIELD_LABELS = {
    "id": "Receipt no.", "received_date": "Received date", "amount": "Amount received",
    "payer": "Received from", "payer_phone": "Phone", "payer_address": "Address",
    "method": "Payment method", "reference": "Invoice / reference", "notes": "Notes",
    "status": "Status", "created_at": "Recorded at (UTC)", "voided_at": "Voided at (UTC)",
    "card_brand": "Card type", "card_last4": "Card ending", "pos_transaction_id": "Bank transaction",
    "pos_authorization_code": "Approval code", "pos_terminal_id": "Terminal ID", "pos_provider_id": "POS provider",
}
BANK_FIELDS = {"card_brand", "card_last4", "pos_transaction_id", "pos_authorization_code", "pos_terminal_id", "pos_provider_id"}
FONTS = ("Arial", "Segoe UI", "Consolas")


@dataclass
class ReceiptDesign:
    merchant_name: str = ""
    merchant_address: str = ""
    merchant_phone: str = ""
    title: str = "PAYMENT RECEIPT"
    footer: str = "Thank you for your payment."
    font_family: str = "Arial"
    font_size: int = 10
    margin_mm: int = 4
    header_alignment: str = "center"
    separator: str = "dashed"
    logo_data: str = ""
    field_order: list[str] = field(default_factory=lambda: list(FIELD_LABELS))
    labels: dict[str, str] = field(default_factory=lambda: FIELD_LABELS.copy())

    def validate(self) -> None:
        for name, limit in (("merchant_name", 120), ("merchant_address", 500), ("merchant_phone", 80),
                            ("title", 100), ("footer", 500)):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{name.replace('_', ' ').title()} must be {limit} characters or fewer.")
        if not self.title.strip():
            raise ValueError("Enter a receipt title.")
        if self.font_family not in FONTS or self.header_alignment not in ("left", "center", "right"):
            raise ValueError("Choose a font and alignment from the lists.")
        if type(self.font_size) is not int or not 8 <= self.font_size <= 14:
            raise ValueError("Font size must be a whole number from 8 to 14.")
        if type(self.margin_mm) is not int or not 1 <= self.margin_mm <= 6:
            raise ValueError("Margins must be a whole number from 1 to 6 mm.")
        if self.separator not in ("dashed", "solid", "none"):
            raise ValueError("Choose a separator from the list.")
        if (not isinstance(self.field_order, list) or len(self.field_order) != len(FIELD_LABELS)
                or any(not isinstance(key, str) for key in self.field_order)
                or set(self.field_order) != set(FIELD_LABELS)):
            raise ValueError("The design must include every receipt field exactly once.")
        if not isinstance(self.labels, dict) or set(self.labels) != set(FIELD_LABELS):
            raise ValueError("Every receipt field needs a label.")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 80 for value in self.labels.values()):
            raise ValueError("Field labels must contain 1 to 80 characters.")
        if not isinstance(self.logo_data, str) or len(self.logo_data) > 2_800_000:
            raise ValueError("The logo must be a PNG, JPEG or GIF image smaller than 2 MB.")
        if self.logo_data:
            try:
                prefix, payload = self.logo_data.split(",", 1)
                image = base64.b64decode(payload, validate=True)
                mime = image_mime(image)
                if prefix != f"data:{mime};base64":
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("The logo must be a valid PNG, JPEG or GIF image.") from None

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "ReceiptDesign":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError
            known = {item.name for item in fields(cls)}
            if set(data) - known:
                raise ValueError
            # Preserve saved business details, logo and labels when a new version adds fields.
            if "field_order" in data:
                order = data["field_order"]
                if (not isinstance(order, list) or any(not isinstance(key, str) for key in order)
                        or len(order) != len(set(order)) or set(order) - set(FIELD_LABELS)):
                    raise ValueError
                data["field_order"] = order + [key for key in FIELD_LABELS if key not in order]
            if "labels" in data:
                if not isinstance(data["labels"], dict) or set(data["labels"]) - set(FIELD_LABELS):
                    raise ValueError
                data["labels"] = {**FIELD_LABELS, **data["labels"]}
            result = cls(**data)
            result.validate()
            return result
        except (ValueError, TypeError):
            raise ValueError("The saved receipt design is invalid. Open Receipt designer to reset it.") from None


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    raise ValueError("Choose a PNG, JPEG or GIF logo.")


def load_logo(path: Path) -> str:
    with Path(path).open("rb") as image:
        data = image.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Choose a logo smaller than 2 MB.")
    return f"data:{image_mime(data)};base64," + base64.b64encode(data).decode("ascii")


def receipt_values(receipt, currency: str) -> dict[str, str]:
    row = dict(receipt)
    values = {key: str(row.get(key) or "—") for key in FIELD_LABELS}
    values.update(id=f"#{row['id']}", amount=f"{currency} {format_amount(row['amount_minor'], currency)}",
                  status="VOIDED — excluded from totals" if row.get("voided_at") else "Recorded",
                  voided_at=row.get("voided_at") or "Not voided")
    if row.get("method") == BANK_METHOD:
        values["card_brand"] = row.get("card_brand") or "Not supplied by terminal"
        last4 = row.get("card_last4", "")
        values["card_last4"] = "**** " + last4 if len(last4) == 4 else "Not supplied by terminal"
    return values


def render_receipts_html(receipts, currency: str, design: ReceiptDesign, *,
                         auto_print: bool = False, reprint: bool = False) -> str:
    design.validate()
    receipts = list(receipts)
    if not receipts:
        raise ValueError("Select at least one receipt to print.")
    sections, page_rules = [], []
    for index, row in enumerate(receipts, 1):
        values = receipt_values(row, currency)
        blocks = []
        for key in design.field_order:
            if key in BANK_FIELDS and dict(row).get("method") != BANK_METHOD:
                continue
            blocks.append(f'<div class="field {"amount" if key == "amount" else ""}">'
                          f'<span class="label">{escape(design.labels[key])}: </span>'
                          f'<span class="value" dir="auto">{escape(values[key])}</span></div>')
        logo = f'<img class="logo" alt="Business logo" src="{design.logo_data}">' if design.logo_data else ""
        badge = '<div class="badge">REPRINT / COPY</div>' if reprint else ""
        if dict(row).get("voided_at"):
            badge += '<div class="badge">VOIDED RECEIPT</div>'
        merchant = "".join(f'<div dir="auto">{escape(value)}</div>' for value in
                           (design.merchant_name, design.merchant_address, design.merchant_phone) if value)
        sections.append(f'<section class="receipt" id="receipt-{index}" style="page:receipt{index}">'
                        f'<header>{logo}{merchant}<h1>{escape(design.title)}</h1>{badge}</header>'
                        f'<div class="fields">{"".join(blocks)}</div>'
                        f'<footer dir="auto">{escape(design.footer)}</footer></section>')
        # Valid static paper sizes work even if a browser disables the measurement script.
        chars = sum(len(value) + len(design.labels[key]) for key, value in values.items())
        height = math.ceil(80 + chars / 28 * design.font_size * 0.5 + (25 if logo else 0))
        page_rules.append(f"@page receipt{index} {{ size:80mm {height}mm; margin:0; }}")
    nonce = secrets.token_urlsafe(18)
    border = "0" if design.separator == "none" else f"1px {design.separator} #000"
    script = r"""
async function preparePages() {
    if (document.fonts) await document.fonts.ready;
    await Promise.all(Array.from(document.images, image => image.decode().catch(() => {})));
    const ruler = document.createElement('div');
    ruler.style.cssText = 'position:absolute;height:100mm;width:1px;visibility:hidden';
    document.body.appendChild(ruler);
    const pixelsPerMm = ruler.getBoundingClientRect().height / 100;
    ruler.remove();
    const rules = Array.from(document.querySelectorAll('.receipt'), (sheet, i) => {
        const height = Math.ceil(sheet.getBoundingClientRect().height / pixelsPerMm) + 2;
        return '@page receipt' + (i + 1) + '{size:80mm ' + height + 'mm;margin:0;}';
    }).join('\n');
    document.getElementById('page-sizes').textContent = rules;
}
async function printReceipts() { await preparePages(); window.print(); }
document.getElementById('print-button').addEventListener('click', printReceipts);
window.addEventListener('load', () => { preparePages().then(() => {
    if (document.body.dataset.autoPrint === 'yes') window.print();
}); });
"""
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'">
<title>Receipt Recorder — 80 mm print</title>
<style>
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:#e9eef3; color:#000; font-family:'{design.font_family}',sans-serif; }}
.toolbar {{ max-width:700px; margin:0 auto 20px; padding:16px; background:white; font:14px Arial,sans-serif; }}
.toolbar button {{ padding:10px 18px; cursor:pointer; }}
.receipt {{ width:80mm; padding:{design.margin_mm}mm; margin:0 auto 18px; background:white; font-size:{design.font_size}pt; line-height:1.35; overflow-wrap:anywhere; box-shadow:0 2px 8px #b6bec7; }}
header {{ text-align:{design.header_alignment}; white-space:pre-wrap; }}
header div,footer,.value {{ unicode-bidi:plaintext; }}
h1 {{ font-size:1.25em; margin:3mm 0; }}
.logo {{ display:block; max-width:40mm; max-height:24mm; margin:0 auto 2mm; object-fit:contain; }}
.badge {{ font-weight:bold; border:1px solid black; padding:1mm; margin:2mm 0; }}
.fields {{ border-top:{border}; padding-top:2mm; }}
.field {{ padding:1mm 0; white-space:pre-wrap; }}
.label {{ font-weight:bold; }}
.amount {{ font-size:1.2em; border-top:{border}; border-bottom:{border}; margin:2mm 0; padding:2mm 0; }}
footer {{ border-top:{border}; margin-top:3mm; padding-top:3mm; text-align:{design.header_alignment}; white-space:pre-wrap; }}
@page {{ size:80mm 250mm; margin:0; }}
@media print {{
 html,body {{ width:80mm; margin:0; padding:0; background:white; }}
 .toolbar {{ display:none; }}
 .receipt {{ margin:0; box-shadow:none; break-after:page; }}
 .receipt:last-child {{ break-after:auto; }}
}}
</style><style id="page-sizes">{"".join(page_rules)}</style></head>
<body data-auto-print="{'yes' if auto_print else 'no'}">
<div class="toolbar"><button id="print-button">Print receipts</button>
<p>Choose your thermal printer and 80 mm paper. Use 100% scale and turn browser headers and footers off.
Each selected receipt prints separately. Close this tab when finished.</p></div>
{"".join(sections)}<script nonce="{nonce}">{script}</script></body></html>'''


def open_receipts(receipts, currency: str, design: ReceiptDesign, output_dir: Path, *,
                  auto_print: bool = True, reprint: bool = False) -> Path:
    html = render_receipts_html(receipts, currency, design, auto_print=auto_print, reprint=reprint)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Only expire documents generated by this app, never receipt databases or user files.
    for old in output_dir.glob("receipt-print-*.html"):
        try:
            if old.stat().st_mtime < time.time() - 7 * 86400:
                old.unlink()
        except OSError:
            pass
    path = output_dir / f"receipt-print-{uuid4().hex}.html"
    path.write_text(html, encoding="utf-8")
    if not webbrowser.open(path.resolve().as_uri(), new=2):
        raise OSError(f"Could not open your browser. Open this file to print: {path}")
    return path
