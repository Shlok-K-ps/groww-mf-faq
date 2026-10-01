import re

import pytest

from rag import sources
from rag.templates import HIDDEN_USER_MESSAGE, SEBI_SID, render, strip_links, to_text

KINDS = ["fact", "concept", "howto", "advice", "performance", "mixed", "clarify", "clarify_field", "unsure", "not_found", "out_of_scope", "pii_block",
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


def test_mixed_keeps_the_fact_citation_as_its_only_link():
    r = render("mixed", answer="Groww ELSS Tax Saver Fund has a lock-in period of 3 years.", source_id="S03", page=1)
    out = to_text(r)
    assert out.count("Source:") == 1 and re.findall(r"https?://\S+", out) == [r.source_url]
    assert "SEBI's investor website" in r.refusal_text and "http" not in r.refusal_text


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


def test_welcome_uses_official_names():
    from rag.templates import UI
    assert "Large Cap" in UI["welcome"] and "Small Cap" in UI["welcome"] and "Largecap" not in UI["welcome"]


ANSWER_KINDS = ["fact", "concept", "howto", "mixed", "advice", "out_of_scope", "performance"]


@pytest.mark.parametrize("kind", KINDS)
def test_never_more_than_one_link_per_response(kind):
    r = _make(kind)
    urls = re.findall(r"https?://[^\s]+", to_text(r))
    assert len(urls) <= 1 and len(r.links()) <= 1
    assert urls == r.links()                                  # the text form shows exactly what links() says


@pytest.mark.parametrize("kind", ANSWER_KINDS)
def test_every_answer_has_exactly_one_link(kind):
    r = _make(kind)
    assert len(r.links()) == 1 and len(re.findall(r"https?://[^\s]+", to_text(r))) == 1


def test_excel_sources_are_labelled_as_downloads():
    assert sources.label("S10").endswith("Excel file") and sources.label("S11").endswith("Excel file")
    assert "Excel" not in sources.label("S01") and "Excel" not in sources.label("S14")
    assert render("fact", answer="TER is 1.69%.", source_id="S10").source_label.endswith("\u00b7 Excel file")


def test_disclaimer_doc_matches_the_ui_text_exactly():
    from rag.templates import UI
    doc = open("docs/disclaimer.md", encoding="utf-8").read()
    assert f"**{UI['banner_head']}** {UI['banner_body']}" in doc and UI["footer"] in doc


# ----------------------------------------------------------------------------- refusal footer, system notices, PII text
REFUSALS = ["advice", "out_of_scope", "performance"]
NOTICES = ["clarify", "clarify_field", "unsure", "pii_block", "service_unavailable", "too_long"]


@pytest.mark.parametrize("kind", REFUSALS)
def test_refusals_carry_exactly_one_link_and_that_sources_date(kind):
    r = render(kind, scheme="Groww Large Cap Fund")
    out = to_text(r)
    assert len(re.findall(r"https?://\S+", out)) == 1 and len(r.links()) == 1
    link_source = "S09" if kind == "performance" else SEBI_SID
    assert f"Last updated from sources: {sources.last_updated(link_source)}" in out and r.last_updated == sources.last_updated(link_source)


def test_advice_and_out_of_scope_link_the_sebi_site():
    for kind in ("advice", "out_of_scope"):
        r = render(kind)
        assert r.refusal_url == sources.source_url(SEBI_SID) and r.links() == [r.refusal_url]
        assert to_text(r).endswith("Last updated from sources: " + sources.last_updated(SEBI_SID))


@pytest.mark.parametrize("kind", NOTICES)
def test_system_notices_carry_no_link_and_no_citation(kind):
    r = render(kind, scheme="Groww Large Cap Fund")
    out = to_text(r)
    assert "http" not in out and "Last updated" not in out and "Source:" not in out and r.links() == []


def test_pii_notice_is_exactly_the_approved_two_sentences():
    from rag.validate import count_sentences
    t = render("pii_block").text
    assert t == ("For your safety, please don't share personal details like PAN, Aadhaar, phone, email, OTP or folio numbers — "
                 "I don't need or store them. Ask your question without them and I'll help.")
    assert count_sentences(t) == 2
