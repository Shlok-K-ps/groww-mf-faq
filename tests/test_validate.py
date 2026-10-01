import pytest

from rag.validate import count_sentences, validate

LC_TER = {"chunk_id": "S10-001", "source_id": "S10", "scheme": "Groww Large Cap Fund", "doc_type": "TER", "page": 1,
          "text": "Total Expense Ratio (TER) of Groww Large Cap Fund as on 30 Sep 2026:\nDirect Plan - Total TER (%): 1.69\n"
                  "Regular Plan - Total TER (%): 2.71"}
MC_TER = dict(LC_TER, chunk_id="S10-003", scheme="Groww Multicap Fund",
              text="Total Expense Ratio (TER) of Groww Multicap Fund as on 30 Sep 2026:\nDirect Plan - Total TER (%): 1.12\n"
                   "Regular Plan - Total TER (%): 2.77")
LOCK = {"chunk_id": "S19-001", "source_id": "S19", "scheme": "ALL", "doc_type": "Education", "page": 1,
        "text": "ELSS are tax-saver mutual funds with a lock-in period of three years. You cannot redeem before 3 years."}
CH = [LC_TER, MC_TER, LOCK]


@pytest.mark.parametrize("text,n", [
    ("One sentence.", 1),
    ("The TER is 0.65% p.a. for the Direct plan.", 1),                       # decimals are not sentence ends
    ("Minimum is Rs. 500 and in multiples of Re. 1/- thereafter.", 1),       # abbreviations
    ("Managed by Mr. Anupam Tiwari and Mr. Nikhil Satam.", 1),
    ("a) 1% within 1 year. b) NIL after 1 year.", 2),                        # list markers
    ("One. Two. Three.", 3),
    ("One. Two. Three. Four.", 4),
    ("Is it? Yes! Done.", 3),
])
def test_count_sentences(text, n):
    assert count_sentences(text) == n


def test_valid_answer_passes():
    ok, why = validate("Groww Large Cap Fund's Direct plan TER is 1.69%.", "S10", CH, ["Groww Large Cap Fund"])
    assert ok, why


def test_injected_other_scheme_ter_is_rejected():
    # model "answers" for Large Cap but smuggles in the Multicap TER (2.77) while citing the Large Cap chunk
    ok, why = validate("Groww Large Cap Fund's Regular plan TER is 2.77%.", "S10", [LC_TER], ["Groww Large Cap Fund"])
    assert not ok and any("number not in cited source" in w for w in why)


def test_naming_an_unasked_scheme_is_rejected():
    ok, why = validate("Groww Multicap Fund's Direct plan TER is 1.69%.", "S10", [LC_TER], ["Groww Large Cap Fund"])
    assert not ok


def test_citation_must_be_among_retrieved():
    ok, why = validate("ELSS has a lock-in period of three years.", "S99", CH)
    assert not ok and "citation not among retrieved sources" in why


@pytest.mark.parametrize("answer,expect", [
    ("You should invest in ELSS, a lock-in period of three years applies.", "advice word"),
    ("ELSS is the best option with a lock-in period of three years.", "advice word"),
    ("ELSS lock-in is three years. See https://example.com", "link or markup"),
    ("ELSS lock-in is three years. [more](http://x.y)", "link or markup"),
    ("ELSS lock-in is three years. One. Two. Three.", "more than 3 sentences"),
    ("ELSS lock-in is 5 years.", "number not in cited source"),
])
def test_rejections(answer, expect):
    ok, why = validate(answer, "S19", CH)
    assert not ok and any(expect in w for w in why), why


def test_empty_rejected():
    assert validate("", "S19", CH) == (False, ["empty"])
