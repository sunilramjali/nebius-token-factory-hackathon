import re

from test_agent import POLICY, prop
from test_report_wording import BANNED, MAX_WORDS, sentences
from test_webapp import ODD

from agent.assistant import LEGAL_PATTERNS
from agent.help import (
    ASSESSOR,
    TRADE_BY_CODE,
    TRADES,
    draft_quote_request,
    find_help,
    trade_for,
)
from agent.tools import Toolbox
from report.portfolio_report import Step

# Every improvement code that appears on a route in the snapshot (route_steps, 2026-11-24).
CODES_IN_DATA = "U Q G E W1 A B A2 L2 I W A3 N C D W2 T2 O F T O3 Y L X B4 S V M Z1 P U1 H".split()


def step(code, summary="Work", lo=100, hi=200, counts=True):
    return Step(1, summary, 5, lo, hi, 70, "C", counts, code)


def test_every_code_in_the_data_has_a_trade():
    assert set(CODES_IN_DATA) <= set(TRADE_BY_CODE)
    assert trade_for(step(None)).key == "general"  # unknown measure: someone to plan it


def test_find_help_orders_trades_and_ends_with_assessor():
    p = prop(steps=[step("A", "Loft insulation"), step("I", "New boiler", counts=False)])
    help_ = find_help(p, POLICY)
    trades = [n["trade"] for n in help_["needed"]]
    assert trades == ["insulation", "gas", "assessor"]
    assert help_["needed"][0]["for"] == ["Loft insulation"]
    assert all(n["link"].startswith("https://") for n in help_["needed"])


def test_borderline_or_many_measures_adds_coordinator_first():
    p = prop(steps=[step("A"), step("B"), step("E")])
    assert find_help(p, POLICY)["needed"][0]["trade"] == "general"
    border = prop(borderline=True)
    assert find_help(border, POLICY)["needed"][0]["trade"] == "general"


def test_cannot_reach_c_starts_with_a_survey_and_done_needs_nothing():
    cannot = prop(lock=False, reaches_c=False, exempt_reason="cannot_reach_c")
    assert find_help(cannot, POLICY)["needed"][0]["trade"] == "assessor"
    assert find_help(prop(score=72, lock=False), POLICY)["needed"] == []


def test_boiler_route_used_when_its_the_one_within_cap():
    p = prop(steps=[step("Q", "Wall insulation")])
    p.lock_in_needs_boiler = True
    p.alternative = [step("I", "New boiler", counts=False)]
    assert [n["trade"] for n in find_help(p, POLICY)["needed"]] == ["gas", "assessor"]


def test_draft_lists_only_that_trades_work_with_costs_and_policy_target():
    p = prop(steps=[step("A", "Loft insulation", 100, 350), step("I", "New boiler")])
    d = draft_quote_request(p, ODD, "insulation")  # ODD policy: C starts at 70
    assert "Loft insulation (indicative cost £100 to £350)" in d["body"]
    assert "New boiler" not in d["body"]
    assert "C (70 or above)" in d["body"] and "[add the address]" in d["body"]
    assert d["subject"] == "Quote request: Loft insulation"


def test_draft_follows_style_guide_and_has_no_register_address():
    p = prop()  # has register_address="1 SECRET ROAD, E13 0AA"
    for key in [*TRADES, ASSESSOR.key]:
        body = draft_quote_request(p, POLICY, key)["body"]
        assert "SECRET" not in body and "E13" not in body
        for s in sentences(body):
            assert len(s.split()) <= MAX_WORDS, s
        for pat in BANNED + LEGAL_PATTERNS:
            assert not re.search(pat, body), (pat, key)


def test_unknown_trade_is_an_error_not_a_guess():
    out = draft_quote_request(prop(), POLICY, "roofer")
    assert "error" in out and "insulation" in out["trades"]


def test_toolbox_routes_new_tools_by_label():
    tb = Toolbox([prop()], POLICY)
    assert tb.call("find_help", {"home": "flat a"})["home"] == "Flat A"
    d = tb.call("draft_quote_request", {"home": "Flat A", "trade": "assessor"})
    assert d["subject"] == "Quote request: new EPC assessment"


def test_help_list_escapes_and_links_to_registers():
    from webapp.view import help_list

    needed = find_help(prop(steps=[step("A", "<b>Loft</b>")]), POLICY)["needed"]
    out = help_list(needed)
    assert "<b>Loft</b>" not in out and "&lt;b&gt;loft&lt;/b&gt;" in out
    assert "https://www.trustmark.org.uk/find-a-tradesperson" in out
    assert "https://www.gov.uk/get-new-energy-certificate" in out


def test_fill_draft_uses_landlord_details_and_keeps_slots_if_blank():
    from agent.help import fill_draft

    body = draft_quote_request(prop(), POLICY, "assessor")["body"]
    filled = fill_draft(body, name="Sam", address="1 My Road", phone="07700 900000")
    assert "1 My Road" in filled and "Sam" in filled and filled.endswith("07700 900000")
    assert "[add the address]" not in filled and "[your name]" not in filled
    assert fill_draft(body) == body  # nothing typed: placeholders stay


def test_mailto_has_no_recipient_and_encodes_the_message():
    from agent.help import mailto

    link = mailto("Quote request: Loft", "Hello,\n\nA & B")
    assert link.startswith("mailto:?subject=Quote%20request%3A%20Loft&body=")
    assert "%0A" in link and "%26" in link and "@" not in link


def test_coordinator_draft_lists_the_whole_job():
    # Regression: a coordinator added for a borderline home got a draft with no work listed.
    p = prop(
        steps=[step("A", "Loft insulation"), step("E", "Low energy lighting")], borderline=True
    )
    assert find_help(p, POLICY)["needed"][0]["trade"] == "general"
    body = draft_quote_request(p, POLICY, "general")["body"]
    assert "help planning this work" in body
    assert "- Loft insulation" in body and "- Low energy lighting" in body
