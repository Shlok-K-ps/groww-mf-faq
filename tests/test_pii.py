import pytest

from rag.pii import contains_pii, normalize, scan

BLOCK = [
    "My PAN is ABCDE1234F, what's my SIP status?",
    "my pan is abcde1234f",                                   # case-insensitive
    "PAN: XXXXX1234X",                                        # masked PAN shape
    "pan number 1234",                                        # label + value
    "Aadhaar 2345 6789 0123",                                 # spaced
    "my aadhaar is 2345-6789-0123",                           # hyphenated
    "2345 6789 0123",                                         # synthetic spaced Aadhaar, no label
    "aadhaar XXXX XXXX 1234",                                 # masked
    "Call me on +91 98765 43210",
    "my number is 9876543210",
    "phone: 098765-43210",
    "+91-9876543210 please check",
    "reach me at john.doe@example.com",
    "email me: john [at] example [dot] com",
    "My OTP is 482913",
    "482913 is my otp",
    "one time password 123456",
    "My folio is 12345/67",
    "folio number 1234567",
    "folio 12345678 balance?",
    "account number 123456789012",
    "a/c no. XXXX1234 status",
    "my acct: 50100123456789",
    "My PAN​ is ABCDE1234F",                             # zero-width char inside
    "ＡＢＣＤＥ１２３４Ｆ",  # full-width ABCDE1234F
    "aadhaar २३४५ ६७८९ ०१२३",  # Devanagari digits
]

PASS = [
    "Is the minimum SIP ₹500?",                          # negative control
    "Exit load if I redeem within 365 days?",                # negative control
    "What is the expense ratio of Groww Large Cap Fund (Direct)?",
    "What was the TER on 30 Sep 2026?",
    "Largecap NAV on 30-09-2026",
    "I want to invest ₹5,00,000 in a lumpsum",
    "SIP of 10000 per month for 120 months",
    "What is the lock-in period for Groww ELSS Tax Saver Fund?",
    "How do I download my capital-gains statement?",
    "How do I get a CAS?",
    "Which account statement shows my capital gains?",       # 'account' without a value
    "Exit load is 1.00% for 365 days",
    "What changed on 01.10.2026?",
    "Is Groww Small Cap Fund 2026 launch date known?",
]


@pytest.mark.parametrize("msg", BLOCK)
def test_blocks(msg):
    assert contains_pii(msg), msg


@pytest.mark.parametrize("msg", PASS)
def test_passes(msg):
    assert not contains_pii(msg), msg


def test_scan_never_returns_the_value():
    cats = scan("My PAN is ABCDE1234F and mobile 9876543210")
    assert set(cats) >= {"pan", "phone"}
    assert all("ABCDE" not in c and "9876" not in c for c in cats)


def test_normalize():
    assert normalize("  a​ b – c  ") == "a b - c"
    assert normalize("１２") == "12"
