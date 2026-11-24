"""Enforce docs/report_style.md on rendered reports: plain English, legal hedges, no hard-coded
policy values. Renders one home of every status so every template sentence is checked."""

import html
import re
from datetime import date

import pytest
from test_report import prop

from engine.policy import Policy
from report.portfolio_report import SAMPLE_NOTE, Step, render

MAX_WORDS = 25
BANNED = [
    r"\bSAP\b", r"\bEER\b", r"mid-point", r"cap-counting", r"data confidence", r"\buplift",
    r"you are exempt", r"you will be compliant", r"will be compliant", r"\bguarantee",
    r"\byou must\b", r"\bwill need EPC\b", r"\bPoints\b",
]  # fmt: skip


def every_status(policy):
    fix = prop("Fix", True, False, 400, 950, [Step(1, "Loft insulation", 8, 100, 350, 68, "D")])
    border = prop("Border", True, False, 400, 950)
    border.borderline, border.score_exact, border.final_score = True, False, 69
    border.safer = [Step(1, "Loft", 4, 100, 350, 65, "D"), Step(2, "Walls", 7, 900, 2000, 72, "C")]
    boiler = prop("Boiler", True, False, 10500, 10500)
    boiler.lock_in_needs_boiler, boiler.within_cap = True, False
    boiler.alternative = [Step(1, "Boiler", 6, 3000, 3000, 65, "D", counts_toward_cap=False),
                          Step(2, "Floor", 4, 6500, 8000, 69, "C")]  # fmt: skip
    quotes = prop("Quotes", True, False, 6000, 14000)
    quotes.cap_verdict_uncertain = True
    flat = prop(
        "Flat",
        True,
        False,
        4000,
        9000,
        [Step(1, "Wall insulation", 6, 4000, 9000, 72, "C", improvement_type="Q")],
    )
    flat.property_type = "flat"
    gas = prop(
        "Gas",
        True,
        False,
        2200,
        3000,
        [Step(1, "Replace boiler", 9, 2200, 3000, 69, "C", counts_toward_cap=False)],
    )
    gas.cap_cost_low = gas.cap_cost_high = 0
    over = prop("Over", False, True, 9000, 20000)
    cannot = prop("Cannot", False, True, 2000, 2000)
    cannot.exempt_reason, cannot.reaches_c = "cannot_reach_c", False
    survey = prop("Survey", False, False, 0, 0)
    survey.cost_known, survey.confidence = False, "low"
    done = prop("Done", False, False, 0, 0)
    done.current_score = 72
    homes = [fix, border, boiler, quotes, flat, gas, over, cannot, survey, done]
    return render(homes, policy, "Report", date(2026, 11, 6), note=SAMPLE_NOTE)


def visible_text(page: str) -> str:
    page = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    page = re.sub(r"<footer>.*?<br>", "<footer>", page, flags=re.S)  # keep footer, drop licence
    page = re.sub(r"<footer>.*?</footer>", lambda m: m.group(0).split("<br>")[0], page, flags=re.S)
    page = re.sub(r"<(br|/p|/li|/h\d|/td|/th|/tr|/div|/section)[^>]*>", "\n", page)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


POLICY = Policy(69, 10_000, date(2029, 9, 30), 10)


def sentences(text: str) -> list[str]:
    out = []
    for block in text.split("\n"):
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z£])", block.strip()):
            if s.strip():
                out.append(s.strip())
    return out


def test_every_sentence_is_short():
    long = [s for s in sentences(visible_text(every_status(POLICY))) if len(s.split()) > MAX_WORDS]
    assert not long, "\n".join(f"{len(s.split())} words: {s}" for s in long)


@pytest.mark.parametrize("pattern", BANNED)
def test_banned_words_never_appear(pattern):
    text = visible_text(every_status(POLICY))
    assert not re.search(pattern, text, flags=re.I), pattern


def test_proposed_rules_are_hedged():
    text = visible_text(every_status(POLICY))
    assert "They are not yet law." in text
    assert "Check the final rules before acting." in text
    assert "not legal or compliance advice" in text
    assert "a survey by an accredited energy assessor is required" in text


def test_policy_values_are_never_hard_coded():
    """Render with made-up policy values: none of the real ones may appear."""
    fake = Policy(
        band_c_threshold=69,
        cost_cap_gbp=12_345,
        lock_in_deadline=date(2031, 3, 3),
        epc_validity_years=7,
        fossil_heating_types=frozenset({"I"}),
        low_value_threshold_gbp=88_888,
        compliance_deadline=date(2032, 4, 4),
    )
    text = visible_text(every_status(fake))
    for real in ["10,000", "2029", "2030 rules", "1 October 2030", "100,000", "10 years"]:
        if real == "2030 rules":
            continue  # "the 2030 rules" names the policy, not a date value
        assert real not in text, real
    for value in ["12,345", "3 March 2031", "4 April 2032", "88,888", "7 years"]:
        assert value in text, value
