"""Turn a screenshot (or an SMS) into structured fields.

IMPORTANT: nothing extracted here is trusted. The transaction reference is only used as a
*key* to look up the matching incoming payment in `bank_credits` (see verify.py).

The regexes below are deliberately generic. Calibrate them against 30-50 real screenshots /
SMS from each provider you accept (see README, "Calibrating the parsers").
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from PIL import Image, ImageOps

# ---- reference normalisation -------------------------------------------------------------

_CONFUSABLE = str.maketrans("OILSBZQ", "0115820")


def normalize_ref(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def canon_ref(s: str) -> str:
    """Fold characters OCR commonly confuses (O/0, I/1/L, S/5, B/8, Z/2, Q/0)."""
    return normalize_ref(s).translate(_CONFUSABLE)


# ---- parsing -----------------------------------------------------------------------------

# "Transaction ID: ABC123XYZ9", "Ref No DKL1234567", "Receipt number is ..." (value must contain a digit)
_LABELLED_REF = re.compile(
    r"(?:transaction\s*(?:id|no\.?|number)|trans(?:action)?\.?\s*(?:id|no\.?)|txn\s*(?:id|no\.?)?"
    r"|ref(?:erence)?\.?\s*(?:id|no\.?|number)?|receipt\s*(?:id|no\.?|number))"
    r"\s*(?:is|:|-|#|=)?\s*((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,20})",
    re.I,
)
# Fallback: any 9-16 char token with both letters and digits (excludes phone numbers and dates).
_GENERIC_REF = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{9,16}\b")

_NUM = r"(\d[\d,]*(?:\.\d{1,2})?)"
_AMOUNT_PATTERNS = [
    re.compile(
        r"(?:settled\s+amount|total\s+paid\s+amount|total\s+amount|amount\s+paid|amount\s+sent|transferred\s+amount|amount|total)[^\d]{0,25}?"
        + _NUM,
        re.I,
    ),
    re.compile(r"(?:ETB|Birr|BR)\.?\s*-?" + _NUM, re.I),
    re.compile(_NUM + r"\s*\(?\s*(?:ETB|Birr|BR)\b\s*\)?", re.I),
]
_SENDER = re.compile(
    r"\b(?:payer\s+name|from|by|sender)\s*:?\s+([A-Za-z][A-Za-z .'\-]{2,60}?)"
    r"(?=\s+(?:(?:on|at|with|via|ref|transaction|trans)\b|\d)|[.,(\r\n]|\s*$)",
    re.I | re.M,
)
_PROVIDERS = {
    "telebirr": "telebirr",
    "commercial bank": "cbe",
    "cbe": "cbe",
    "awash": "awash",
    "dashen": "dashen",
    "abyssinia": "boa",
    "m-pesa": "mpesa",
    "mpesa": "mpesa",
}


def find_transaction_id(text: str) -> str | None:
    m = _LABELLED_REF.search(text)
    if m:
        return normalize_ref(m.group(1))
    m = _GENERIC_REF.search(text.upper())
    return normalize_ref(m.group(0)) if m else None


def find_amount(text: str) -> Decimal | None:
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return Decimal(m.group(1).replace(",", ""))
            except InvalidOperation:
                continue
    return None


def find_sender(text: str) -> str | None:
    m = _SENDER.search(text)
    return m.group(1).strip() if m else None


def find_provider(text: str) -> str | None:
    low = text.lower()
    for key, name in _PROVIDERS.items():
        if key in low:
            return name
    return None


_CBE_URL_RE = re.compile(r"https?://(?:www\.)?mbreciept\.cbe\.com\.et/([A-Za-z0-9_-]+)", re.I)


def fetch_cbe_transaction_detail(token: str) -> dict | None:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://Mb.cbe.com.et/api/v1/transactions/public/transaction-detail/{token}",
            headers={
                "X-App-ID": "d1292e42-7400-49de-a2d3-9731caa4c819",
                "X-App-Version": "0a01980b-9859-1369-8198-59f403820000",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if data and data.get("id"):
                return data
    except Exception:
        pass
    return None


def scan_barcodes(image_or_bytes: bytes | Image.Image) -> list[str]:
    try:
        import zxingcpp
        if isinstance(image_or_bytes, bytes):
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_or_bytes)))
        else:
            img = image_or_bytes
        results = zxingcpp.read_barcodes(img)
        return [r.text for r in results if r.text]
    except Exception:
        return []


@dataclass
class Extracted:
    transaction_id: str | None = None
    amount: Decimal | None = None
    sender: str | None = None
    receiver: str | None = None
    paid_at: str | None = None  # raw text only; never used for decisions
    provider: str | None = None
    raw_text: str = ""
    verified_bank_credit: dict | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount) if self.amount is not None else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Extracted":
        d = dict(d)
        if d.get("amount") is not None:
            d["amount"] = Decimal(d["amount"])
        valid_keys = {
            "transaction_id",
            "amount",
            "sender",
            "receiver",
            "paid_at",
            "provider",
            "raw_text",
            "verified_bank_credit",
        }
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)


def parse_text(text: str) -> Extracted:
    return Extracted(
        transaction_id=find_transaction_id(text),
        amount=find_amount(text),
        sender=find_sender(text),
        provider=find_provider(text),
        raw_text=text,
    )


# ---- extractors --------------------------------------------------------------------------


def find_tesseract_cmd(configured: str = "") -> str | None:
    """Locate tesseract binary on Linux or Windows."""
    if configured and os.path.exists(configured):
        return configured
    in_path = shutil.which("tesseract")
    if in_path:
        return in_path
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tesseract", "tesseract.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__import__("sys").executable)), "tesseract", "tesseract.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def parse_cbe_datetime(data: dict) -> datetime | None:
    """Parse dateTimes[0] from CBE transaction JSON as timezone-aware UTC datetime."""
    dts = data.get("dateTimes")
    if not (isinstance(dts, list) and dts and isinstance(dts[0], str) and dts[0].strip()):
        return None
    try:
        parsed = datetime.fromisoformat(dts[0].strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _try_cbe_url(text: str, raw_text: str | None = None, source: str = "QR") -> Extracted | None:
    cbe_m = _CBE_URL_RE.search(text)
    if not cbe_m:
        return None
    token = cbe_m.group(1)
    data = fetch_cbe_transaction_detail(token)
    if not data or data.get("status") != "COMPLETED":
        return None
    ref = normalize_ref(data["id"])
    amt_str = data.get("amountCredited") or data.get("amountDebited") or "100.00"
    amt = Decimal(str(amt_str))
    sender = data.get("debitAccountHolder") or data.get("creditAccountHolder")
    receiver = data.get("creditAccountNo")
    dt_parsed = parse_cbe_datetime(data)
    received_at = dt_parsed.isoformat() if dt_parsed else None
    return Extracted(
        transaction_id=ref,
        amount=amt,
        sender=sender,
        receiver=receiver,
        provider="cbe",
        raw_text=raw_text if raw_text is not None else f"CBE Verified Receipt ({source}): {text}\nRef: {ref}\nAmount: {amt}",
        verified_bank_credit={
            "reference": ref,
            "amount": str(amt),
            "sender": sender,
            "credit_account": receiver,
            "received_at": received_at,
            "raw_text": f"CBE {source} Verified: {token}",
        },
    )


def has_cbe_url(text: str | None) -> bool:
    return bool(text and _CBE_URL_RE.search(text))


def find_cbe_url(text: str | None) -> str | None:
    if not text:
        return None
    m = _CBE_URL_RE.search(text)
    return m.group(0) if m else None


def extract_cbe_link(text: str) -> Extracted | None:
    """Resolve a pasted CBE receipt link directly against CBE's verification API."""
    return _try_cbe_url(text, raw_text=text, source="LINK")


def extract_pdf(pdf_bytes: bytes) -> Extracted:
    """Extract CBE receipt URL or transaction details from a PDF receipt (pure-Python)."""
    chunks: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt:
                chunks.append(txt)
            annots = page.get("/Annots") or []
            for annot_ref in annots:
                try:
                    annot = annot_ref.get_object()
                    action = annot.get("/A")
                    if action:
                        uri = action.get("/URI")
                        if uri:
                            chunks.append(str(uri))
                except Exception:
                    continue
    except Exception:
        pass

    # Fallback: scan raw PDF bytes for uncompressed CBE URLs
    raw_latin = pdf_bytes.decode("latin-1", errors="ignore")
    for m in _CBE_URL_RE.finditer(raw_latin):
        chunks.append(m.group(0))

    combined = "\n".join(chunks).strip()
    if combined:
        cbe_ex = _try_cbe_url(combined, raw_text=combined, source="PDF")
        if cbe_ex:
            return cbe_ex
        return parse_text(combined)
    return Extracted(raw_text="[PDF unreadable: no text or CBE receipt URL found]")


class TesseractExtractor:
    """Classic OCR with QR/barcode scanning. Uses zxing-cpp natively and tesseract if installed."""

    def __init__(self, langs: str = "eng", tesseract_cmd: str = ""):
        self.langs = langs
        self.tesseract_cmd = find_tesseract_cmd(tesseract_cmd)

    def extract(self, image: bytes) -> Extracted:
        # 1. First, scan for QR codes / barcodes (fast, cryptographic proof directly from bank)
        raw_img = ImageOps.exif_transpose(Image.open(io.BytesIO(image)))
        barcodes = scan_barcodes(raw_img)
        for b in barcodes:
            cbe_ex = _try_cbe_url(b, source="QR")
            if cbe_ex:
                return cbe_ex

        # 2. If no CBE QR code, try Tesseract OCR (gracefully degrade if binary isn't installed on Windows)
        if not self.tesseract_cmd:
            return Extracted(raw_text="[OCR unavailable: no QR code found and Tesseract is not installed]")

        try:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
            img = raw_img.convert("L")
            if img.width < 1000:  # small screenshots OCR badly
                scale = 1000 / img.width
                img = img.resize((1000, int(img.height * scale)), Image.LANCZOS)
            text = pytesseract.image_to_string(img, lang=self.langs)
        except Exception as e:
            return Extracted(raw_text=f"[OCR skipped: {type(e).__name__}: {e}]")

        cbe_ex = _try_cbe_url(text, raw_text=text, source="OCR-URL")
        if cbe_ex:
            return cbe_ex

        return parse_text(text)


class ClaudeVisionExtractor:
    """Vision-model extraction: more tolerant of layouts / languages than plain OCR.

    The model's output is still untrusted input: a screenshot containing text such as
    "ignore previous instructions" can at worst change what *reference* we look up, and a
    reference only yields tickets if it matches real money in the account.
    """

    PROMPT = (
        "This is a screenshot of a mobile-money or bank payment receipt. Reply with ONLY a JSON "
        "object with keys: transaction_id, amount (number, in ETB), sender, receiver, paid_at, "
        "provider. Use null for anything you cannot read clearly. Copy transaction_id exactly, "
        "character by character; do not guess or correct it."
    )

    def __init__(self, model: str, api_key: str = ""):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model

    def extract(self, image: bytes) -> Extracted:
        fmt = (Image.open(io.BytesIO(image)).format or "JPEG").upper()
        media = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(
            fmt, "image/jpeg"
        )
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media,
                                "data": base64.b64encode(image).decode(),
                            },
                        },
                        {"type": "text", "text": self.PROMPT},
                    ],
                }
            ],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        amount = None
        try:
            if data.get("amount") is not None:
                amount = Decimal(str(data["amount"]).replace(",", ""))
        except InvalidOperation:
            pass
        tx = data.get("transaction_id")
        return Extracted(
            transaction_id=normalize_ref(str(tx)) if tx else None,
            amount=amount,
            sender=data.get("sender"),
            receiver=data.get("receiver"),
            paid_at=data.get("paid_at"),
            provider=(str(data["provider"]).lower() if data.get("provider") else None),
            raw_text=text,
        )


def make_extractor(settings):
    if settings.extractor == "claude":
        return ClaudeVisionExtractor(settings.claude_model, getattr(settings, "anthropic_api_key", ""))
    return TesseractExtractor(settings.ocr_langs, getattr(settings, "tesseract_cmd", ""))
