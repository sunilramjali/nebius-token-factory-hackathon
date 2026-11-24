import re
from datetime import date

import pytest
from test_agent import POLICY, prop
from test_report_wording import BANNED, MAX_WORDS, sentences

from agent.assistant import LEGAL_PATTERNS
from agent.tools import Toolbox
from engine.policy import Policy
from webapp.logic import TEXT, needs_input_message, rows_from_entries, table_rows, text

ODD = Policy(70, 12_345, date(2028, 1, 2), 7, compliance_deadline=date(2031, 3, 4))


@pytest.mark.parametrize("key", sorted(TEXT))
def test_ui_text_follows_style_guide(key):
    t = text(key, POLICY)
    for s in sentences(t):
        assert len(s.split()) <= MAX_WORDS, s
    for pat in BANNED + LEGAL_PATTERNS:
        assert not re.search(pat, t), (pat, t)


def test_ui_text_takes_policy_values_from_config():
    assert "£12,345" in text("intro", ODD) and "2 January 2028" in text("fix_early_note", ODD)
    for key, t in TEXT.items():
        assert "10,000" not in t and "2029" not in t and "2030" not in t, key


def test_rows_from_entries_skips_blanks_and_never_uses_address_as_label():
    # Labels go to Nebius, so a blank label must not fall back to the address.
    rows = rows_from_entries(
        [
            {"label": "", "address": "Flat 2, 15 Example Road", "postcode": "e13 0aa"},
            {"label": "", "address": "", "postcode": "", "uprn": ""},
            {"label": "Corner shop flat", "address": "", "uprn": " 12345 "},
        ]
    )
    assert [r.label for r in rows] == ["Home 1", "Corner shop flat"]
    assert rows[0].postcode == "E13 0AA" and rows[0].uprn is None
    assert rows[1].uprn == 12345 and [r.line for r in rows] == [1, 2]


def test_needs_input_message_has_no_html():
    assert "<" not in needs_input_message("ambiguous")
    assert needs_input_message("no_data").startswith("Found on the EPC register")


def test_table_rows_match_assistant_facts():
    cannot = prop("300", "House C", lock=False, exempt=True, reaches_c=False,
                  exempt_reason="cannot_reach_c")  # fmt: skip
    done = prop("400", "House D", score=72, lock=False)
    rows = {r["Home"]: r for r in table_rows(Toolbox([prop(), cannot, done], POLICY))}
    assert rows["Flat A"]["Cheapest route to C"] == "£400–£950"
    assert rows["Flat A"]["Likely within cap"] == "Yes" and rows["Flat A"]["Fix early"] == "Yes"
    assert rows["Flat A"]["Rating gain to C"] == "9"
    # No route to C: no cost, no cap verdict.
    assert rows["House C"]["Cheapest route to C"] == "—"
    assert rows["House C"]["Likely within cap"] == "—"
    assert rows["House D"]["Status"] == "Already C" and rows["House D"]["Rating gain to C"] == "—"
