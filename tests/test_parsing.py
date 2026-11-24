"""Parser tests. The sample texts are ILLUSTRATIVE, not real provider formats:
replace/add real ones from your own receiving account before launch."""
from decimal import Decimal

from app.extract import canon_ref, find_amount, find_provider, find_sender, find_transaction_id, normalize_ref, parse_text
from app.sms import parse_credit_sms


def test_labelled_reference_and_amount():
    t = "Transaction ID: DKL4A7B9Q2\nAmount: ETB 300.00\nTo: Lottery Account"
    assert find_transaction_id(t) == "DKL4A7B9Q2"
    assert find_amount(t) == Decimal("300.00")


def test_reference_on_next_line():
    assert find_transaction_id("Transaction number\nAB12CD34EF") == "AB12CD34EF"


def test_label_followed_by_word_is_not_a_reference():
    # "transaction" has no digit, so it must not be captured as the reference
    assert find_transaction_id("Ref: transaction successful FT24317ABCDE") == "FT24317ABCDE"


def test_phone_numbers_and_dates_are_not_references():
    assert find_transaction_id("Call 0912345678 on 20261116 or 251912345678") is None


def test_amount_variants():
    assert find_amount("Total amount paid 1,250.50 Birr") == Decimal("1250.50")
    assert find_amount("You sent 100 ETB") == Decimal("100")
    assert find_amount("Paid ETB100") == Decimal("100")
    assert find_amount("nothing here") is None


def test_canonical_folding():
    assert canon_ref("DKL4A7B9Q2") == canon_ref("DKI4A7B9O2") == canon_ref("dkl4a7b9q2")
    assert normalize_ref(" dkl-4a7 b9q2 ") == "DKL4A7B9Q2"


def test_provider_and_sender():
    assert find_provider("Sent via Telebirr") == "telebirr"
    assert find_provider("Commercial Bank of Ethiopia") == "cbe"
    assert find_sender("You have received ETB 100.00 from Abebe Kebede on 16/11/2026") == "Abebe Kebede"
    # receipt layout: label, colon, name, newline
    assert find_sender("Transaction successful\nFrom: Abebe Kebede\nAmount: ETB 300.00") == "Abebe Kebede"
    assert find_sender("Sender: Selam Tesfaye\nAmount: 100") == "Selam Tesfaye"


def test_parse_credit_sms_incoming():
    sms = "Dear customer, you have received ETB 200.00 from Abebe Kebede on 16/11/2026. Transaction number is DKL4A7B9Q2."
    c = parse_credit_sms(sms)
    assert c and c.reference == "DKL4A7B9Q2" and c.amount == Decimal("200.00") and c.sender == "Abebe Kebede"


def test_parse_credit_sms_ignores_debits_and_junk():
    assert parse_credit_sms("You have transferred ETB 200.00 to X. Transaction number is DKL4A7B9Q2.") is None
    assert parse_credit_sms("Your OTP is 123456") is None
    assert parse_credit_sms("received a gift!") is None  # no reference / amount


def test_parse_text_roundtrip_json():
    ex = parse_text("Telebirr\nTransaction ID: DKL4A7B9Q2\nAmount ETB 100.00")
    from app.extract import Extracted

    assert Extracted.from_json(ex.to_json()).amount == Decimal("100.00")
