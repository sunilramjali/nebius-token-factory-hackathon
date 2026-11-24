import json
import re
from datetime import date

import pytest
from test_agent import POLICY, prop
from test_report_wording import BANNED, MAX_WORDS, sentences

from agent.assistant import LEGAL_PATTERNS
from agent.tools import Toolbox
from engine.policy import Policy
from webapp.logic import (
    DEMO_FILE,
    TEXT,
    DailyCounter,
    dump_demo,
    load_demo,
    needs_input_message,
    rows_from_entries,
    table_rows,
    text,
)

ODD = Policy(70, 12_345, date(2028, 1, 2), 7, compliance_deadline=date(2031, 3, 4))


@pytest.mark.parametrize("key", sorted(TEXT))
def test_ui_text_follows_style_guide(key):
    t = text(key, POLICY, n=10, home="Home 1", who="an accredited energy assessor",
             address="Flat 2, 15 Example Road, E99 9ZZ", total="£800 to £1,200",
             budget="£8,000", left="£4,000")  # fmt: skip
    for s in sentences(t):
        assert len(s.split()) <= MAX_WORDS, s
    for pat in BANNED + LEGAL_PATTERNS:
        assert not re.search(pat, t), (pat, t)


def test_ui_text_takes_policy_values_from_config():
    assert "2 January 2028" in text("fix_early_note", ODD)
    assert "4 March 2031" in text("problem", ODD) and "£12,345" in text("tile_spend_note", ODD)
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
    assert rows["Flat A"]["Cheapest route to C"] == "£400 to £950"
    assert rows["Flat A"]["Likely within cap"] == "Yes" and rows["Flat A"]["Fix early"] == "Yes"
    assert rows["Flat A"]["Rating gain to C"] == "9"
    # No route to C: no cost, no cap verdict.
    assert rows["House C"]["Cheapest route to C"] == "—"
    assert rows["House C"]["Likely within cap"] == "—"
    assert rows["House D"]["Status"] == "Already C" and rows["House D"]["Rating gain to C"] == "—"


# --- hosted demo -----------------------------------------------------------------------------


def test_demo_round_trip_drops_register_address(tmp_path):
    p = prop()  # has register_address="1 SECRET ROAD, E13 0AA"
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(dump_demo([p])))
    assert "SECRET" not in path.read_text()
    (back,) = load_demo(path)
    assert back.register_address is None
    assert (back.label, back.registration_date, back.steps[0].summary) == (
        p.label, p.registration_date, p.steps[0].summary
    )  # fmt: skip


def test_committed_demo_file_has_no_addresses():
    # Served publicly by the hosted demo: UPRNs and EPC fields only (docs/data_access.md §3).
    raw = DEMO_FILE.read_text()
    assert not re.search(r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b", raw, re.I), "postcode-like"
    assert "register_address" not in raw and "address_line" not in raw
    homes = load_demo()
    assert [p.label for p in homes] == [
        "Flat A",
        "Flat B",
        "Flat C",
        "House A",
        "Flat D",
        "House B",
    ]
    assert len({table_rows(Toolbox(homes, POLICY))[i]["Status"] for i in range(6)}) == 6


def test_daily_counter_caps_and_resets():
    c = DailyCounter(2)
    day = date(2026, 11, 24)
    assert [c.take(day), c.take(day), c.take(day)] == [True, True, False]
    assert c.take(date(2026, 11, 25)) is True  # new day, new allowance


# --- view (HTML pieces) ----------------------------------------------------------------------


def test_view_escapes_landlord_labels():
    from webapp.view import home_card

    p = prop(label="<script>alert(1)</script>")
    out = home_card(p, Toolbox([p], POLICY))
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_every_status_has_a_colour_and_icon():
    from report.portfolio_report import ACTIONS
    from webapp.view import STATUS_STYLE

    assert set(ACTIONS) | {"done"} <= set(STATUS_STYLE)


def test_meter_uses_policy_threshold_and_plain_words():
    from webapp.view import home_card, short_of_c

    p = prop(score=60)
    assert short_of_c(p, 69) == "Rating 60 · 9 short of C"
    assert short_of_c(prop(score=72), 69) == "Rating 72 · C or better"
    out = home_card(p, Toolbox([p], ODD))  # C line comes from the policy, not a literal
    assert "left:70.0%" in out and "width:60.0%" in out
    assert "About £400 to £950" in out


def test_tiles_show_portfolio_counts():
    from webapp.view import tiles

    labels = dict.fromkeys(["below", "fix_early", "over_cap", "spend", "spend_note"], "x")
    out = tiles(Toolbox([prop(), prop("400", "Done", score=72, lock=False)], POLICY), labels)
    assert "1 of 2" in out and "£400 to £950" in out


# --- next steps --------------------------------------------------------------------------


def test_follow_ups_after_find_help_offer_drafts_in_work_order():
    from agent.help import find_help
    from webapp.logic import follow_ups

    p = prop()
    tb = Toolbox([p], POLICY)
    calls = [{"name": "find_help", "args": {"home": "Flat A"}, "result": find_help(p, POLICY)}]
    steps = follow_ups(calls, tb)
    # prop()'s one measure has no EPC code -> "general" (retrofit coordinator), then assessor.
    assert [(s["kind"], s["trade"]) for s in steps] == [("draft", "general"), ("draft", "assessor")]
    assert all(s["home"] == "100" for s in steps) and len(steps) <= 3


def test_follow_ups_after_summary_suggest_the_first_fix_early_home():
    from agent.tools import portfolio_summary
    from webapp.logic import follow_ups

    homes = [prop("400", "Done", score=72, lock=False), prop()]
    tb = Toolbox(homes, POLICY)
    calls = [{"name": "get_portfolio_summary", "args": {},
              "result": portfolio_summary(homes, POLICY)}]  # fmt: skip
    steps = follow_ups(calls, tb)
    labels = [text(s["label_key"], POLICY, home=s["home"]) for s in steps]
    assert labels == ["Who do I call for Flat A?", "What work does Flat A need?"]


def test_no_follow_ups_for_already_c_or_unknown_tools():
    from webapp.logic import follow_ups

    tb = Toolbox([prop()], POLICY)
    done = {"name": "get_property", "args": {}, "result": {"label": "Done", "gap_to_c": 0}}
    assert follow_ups([done, {"name": "get_rules", "args": {}, "result": {}}], tb) == []


def test_nicknames_from_property_type_never_addresses():
    from webapp.logic import nicknames

    assert nicknames(["flat", "house", "flat", None, "maisonette"]) == [
        "Flat A", "House A", "Flat B", "Home A", "Maisonette A"
    ]  # fmt: skip


def test_typed_addresses_use_only_what_the_landlord_typed():
    from lookup.portfolio import PortfolioRow, Resolved
    from webapp.logic import typed_addresses

    typed = Resolved(PortfolioRow(1, "Flat 2, 15 Example Road", "E99 9ZZ", "Mine", None),
                     "matched", 11, options=[(11, "FLAT 2 REGISTER ADDRESS")])  # fmt: skip
    has_pc = Resolved(
        PortfolioRow(2, "3 Example Road E99 9ZZ", "E99 9ZZ", "B", None), "matched", 12
    )
    by_uprn = Resolved(PortfolioRow(3, "", None, "C", 13), "given", 13)
    unmatched = Resolved(PortfolioRow(4, "9 Nowhere", None, "D", None), "no_match", None)
    out = typed_addresses([typed, has_pc, by_uprn, unmatched])
    assert out == {"11": "Flat 2, 15 Example Road, E99 9ZZ", "12": "3 Example Road E99 9ZZ"}
    assert "REGISTER" not in str(out)


def test_demo_details_are_fictional():
    from webapp.logic import DEMO_LANDLORD

    # Ofcom's drama range (07700 900000-900999): can never reach a real person.
    assert DEMO_LANDLORD["phone"].replace(" ", "").startswith("07700900")
    assert 0 <= int(DEMO_LANDLORD["phone"].replace(" ", "")[-3:]) <= 999
