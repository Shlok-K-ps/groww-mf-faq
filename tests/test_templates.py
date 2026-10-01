import re

import pytest

from rag import sources
from rag.templates import HIDDEN_USER_MESSAGE, SEBI_SID, render, strip_links, to_text

KINDS = ["fact", "concept", "howto", "advice", "performance", "mixed", "clarify", "not_found", "out_of_scope", "pii_block",
         "service_unavailable"]


def _make(kind):
    return render(kind, answer="Groww Large Cap Fund's total expense ratio (TER) is 1.69% for the Direct plan.", source_id="S10",
                  scheme="Groww Large Cap Fund")


@pytest.mark.parametrize("kind", KINDS)
def test_every_url_in_output_comes_from_sources_csv(kind):
    allowed = {r["url"] for r in sources.load().values()}
    for url in re.findall(r"https?://[^\s#]+", to_text(_make(kind))):
        assert any(a.startswith(url) for a in allowed), url


def test_fact_format_has_exactly_one_source_and_date_from_csv():
    out = to_text(render("fact", answer="TER is 1.69%.", source_id="S10"))
    assert out.count("Source:") == 1
    assert "Last updated from sources: 30 Sep 2026" in out


def test_pdf_gets_page_anchor_but_xlsx_and_html_do_not():
    assert sources.source_url("S01", 11).endswith(".pdf#page=11")
    assert "#page" not in sources.source_url("S10", 3)          # xlsx
    assert "#page" not in sources.source_url("S14", 1)          # html
    assert sources.source_url("S01").endswith(".pdf")


def test_model_links_and_html_are_stripped():
    t = strip_links("See [this](https://example.com/x) or https://example.com <b>now</b> www.evil.com ok")
    assert "example.com" not in t and "evil" not in t and "<b>" not in t and "this" in t
    r = render("concept", answer="ELSS has a lock-in of 3 years. Visit https://example.com", source_id="S19")
    assert "example.com" not in to_text(r)


def test_advice_refusal_has_sebi_link_and_no_citation_block():
    r = render("advice")
    assert r.refusal_url == sources.source_url(SEBI_SID) and not r.cited
    assert "Source:" not in to_text(r)


def test_mixed_keeps_one_fact_citation_and_a_separate_refusal_link():
    r = render("mixed", answer="Groww ELSS Tax Saver Fund has a lock-in period of 3 years.", source_id="S03", page=1)
    out = to_text(r)
    assert out.count("Source:") == 1 and "Learn more: https://investor.sebi.gov.in/" in out
    assert r.source_url != r.refusal_url


def test_performance_links_factsheet():
    r = render("performance", scheme="Groww Small Cap Fund")
    assert r.source_id == "S09" and "latest factsheet" in r.text


def test_not_found_never_claims_a_source_unless_given_one():
    assert not render("not_found").cited
    r = render("not_found", closest_source_id="S05")
    assert "SID" in r.source_label and "Source:" not in to_text(r)


def test_templates_do_not_accept_user_text():
    # render() has no parameter for raw user input, so refusals/PII notices cannot echo it.
    import inspect
    assert not {"query", "question", "user_text", "message"} & set(inspect.signature(render).parameters)
    assert "personal information" in HIDDEN_USER_MESSAGE


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        render("nope")
