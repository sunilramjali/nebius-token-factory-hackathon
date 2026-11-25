import json
from datetime import date
from types import SimpleNamespace

import pytest

from agent.assistant import (
    DISCLAIMER,
    FALLBACK,
    LEGAL_PATTERNS,
    UNCHECKED_NOTE,
    Assistant,
    drop_sentences,
    legal_conclusions,
    ungrounded_amounts,
)
from agent.tools import TOOL_SPECS, Toolbox, portfolio_summary, property_facts, rules
from engine.policy import Policy
from report.portfolio_report import Property, Step

POLICY = Policy(69, 10_000, date(2029, 9, 30), 10)


def prop(pid="100", label="Flat A", score=60, lock=True, exempt=False, lo=400, hi=950, **kw):
    p = Property(
        property_id=pid, label=label, property_type="house",
        registration_date=date(2023, 6, 1), current_score=score,
        current_band="D" if score < 69 else "C", final_score=71, reaches_c=True,
        cost_low=lo, cost_high=hi, cost_mid=(lo + hi) / 2, within_cap=not exempt,
        confidence="high", lock_in_candidate=lock, likely_exempt_cost_cap=exempt,
        needs_survey=False, notes="",
        steps=[Step(1, "Loft insulation", 11, lo, hi, 71, "C")],
        exempt_reason="over_cap" if exempt else None,
        register_address="1 SECRET ROAD, E13 0AA",
    )  # fmt: skip
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# --- tools ---------------------------------------------------------------------------------


def test_property_facts_come_from_engine_and_report():
    f = property_facts(prop(), POLICY)
    assert f["gap_to_c"] == 9
    assert f["status"] == "Fix early"
    assert f["cheapest_route"]["total_cost"] == "£400 to £950"
    assert f["cheapest_route"]["measures"][0]["rating_gain"] == 11
    assert f["next_action"].startswith("Get quotes for Flat A")
    assert "30 September 2029" in f["next_action"]


def test_tool_output_never_contains_register_address():
    p = prop()
    tb = Toolbox([p], POLICY)
    out = json.dumps([tb.call(t["function"]["name"], {"home": "100"}) for t in TOOL_SPECS])
    assert "SECRET" not in out and "E13" not in out


def test_already_c_has_no_route():
    f = property_facts(prop(score=72, lock=False), POLICY)
    assert f["status"] == "Already C" and f["gap_to_c"] == 0
    assert "cheapest_route" not in f


def test_unknown_uprn_is_refused_not_looked_up():
    out = Toolbox([prop()], POLICY).call("get_property", {"home": "999"})
    assert "error" in out and out["homes"] == ["Flat A"]


def test_get_property_by_label_or_uprn():
    # Regression: a model asked for "Home 4" by label and wrongly told the landlord it wasn't
    # in their portfolio.
    tb = Toolbox([prop()], POLICY)
    assert tb.call("get_property", {"home": "flat a"})["uprn"] == "100"
    assert tb.call("get_property", {"home": "100"})["label"] == "Flat A"
    assert tb.call("get_property", {"uprn": "100"})["label"] == "Flat A"


def test_home_names_match_forgivingly():
    tb = Toolbox([prop(), prop("200", "House B")], POLICY)
    for typed in ["flat-a", "Flat A's", "the flat a", " FLAT  A "]:
        assert tb.home(typed).label == "Flat A", typed
    assert tb.home("Flat B") is None  # different home: no guessing
    assert tb.home("house b").label == "House B"


def test_portfolio_summary_totals():
    s = portfolio_summary([prop(), prop("200", "House B", exempt=True, lock=False,
                                        lo=6000, hi=17000)], POLICY)  # fmt: skip
    assert s["homes_below_c"] == 2 and s["likely_over_cap"] == 1 and s["fix_early"] == 1
    # spend stops at the cap: 400 + 6000 .. 950 + 10000
    assert s["estimated_total_spend"] == "£6,400 to £10,950"


def test_cannot_reach_c_has_no_cheapest_cost():
    p = prop(lock=False, reaches_c=False, exempt_reason="cannot_reach_c")
    home = portfolio_summary([p], POLICY)["homes"][0]
    assert home["status"] == "Can't reach C" and home["cheapest_cost_to_c"] is None


def test_exemption_wording_only_where_relevant():
    # Regression: a model invented an exemption condition when the tool gave no wording.
    over = property_facts(prop(lock=False, exempt=True), POLICY)
    assert over["exemption"].startswith("You may be able to register an exemption")
    assert "isn't published yet" in over["exemption"]
    assert property_facts(prop(), POLICY)["exemption"] is None


def test_total_spend_says_what_it_covers():
    # Regression: a model described the stop-at-cap total as "all cheapest routes added up".
    s = portfolio_summary([prop()], POLICY)
    assert "stopping at the £10,000 cap" in s["estimated_total_spend_covers"]
    assert "not an exemption" in rules(POLICY)["lower_cap_note"]


def test_summary_counts_each_home_once():
    # Regression: "can't reach C" homes were also counted as "likely over the cap".
    cannot = prop("300", "House C", lock=False, exempt=True, reaches_c=False,
                  exempt_reason="cannot_reach_c")  # fmt: skip
    s = portfolio_summary([prop("200", "House B", lock=False, exempt=True), cannot], POLICY)
    assert (s["likely_over_cap"], s["cant_reach_c"]) == (1, 1)


def test_rules_use_policy_values_not_literals():
    odd = Policy(70, 12_345, date(2028, 1, 2), 7, compliance_deadline=date(2031, 3, 4))
    r = rules(odd)
    assert r["cost_cap"] == "£12,345" and r["fix_early_deadline"] == "2 January 2028"
    assert "12,345" in r["summary"] and "not yet law" in r["summary"].lower()


# --- grounding -----------------------------------------------------------------------------


def test_ungrounded_amounts():
    results = [json.dumps({"total_cost": "£400 to £950", "cap": "£10,000"})]
    assert ungrounded_amounts("About £400 to £950, under £10,000 (£10k).", results) == []
    assert ungrounded_amounts("Roughly £1,350 in total.", results) == ["£1,350"]


# --- assistant loop with a fake Nebius client ----------------------------------------------


def _msg(content=None, calls=()):
    tcs = [
        SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=a))
        for i, (n, a) in enumerate(calls)
    ]
    return SimpleNamespace(content=content, tool_calls=tcs or None)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.seen.append(json.loads(json.dumps(kw["messages"])))
        return SimpleNamespace(choices=[SimpleNamespace(message=self.script.pop(0))])


# A tool call first, as real answers have: without one, the agent gets a "look it up" nudge.
LOOKUP = _msg(calls=[("get_property", '{"home": "Flat A"}')])


def bot(script):
    return Assistant(Toolbox([prop()], POLICY), client=FakeClient(script), model="test")


def test_tool_loop_then_answer_with_disclaimer():
    b = bot([_msg(calls=[("get_property", '{"home": "100"}')]),
             _msg("Flat A: loft insulation, about £400 to £950.")])  # fmt: skip
    r = b.ask("What should I do with Flat A?")
    assert r.grounded and [c["name"] for c in r.tool_calls] == ["get_property"]
    assert r.text.endswith(DISCLAIMER)
    # The label says where the numbers come from, and still asks for a survey.
    assert "current EPC certificate" in DISCLAIMER and "accredited assessor" in DISCLAIMER
    tool_msg = b.client.seen[1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c0"


def test_invented_amount_gets_retry_then_flag():
    b = bot([_msg(calls=[("get_property", '{"home": "100"}')]),
             _msg("It costs £1,234."), _msg("Still £1,234.")])  # fmt: skip
    r = b.ask("Cost?")
    assert not r.grounded and UNCHECKED_NOTE in r.text
    assert "aren't in the tool results" in b.client.seen[-1][-1]["content"]


def test_invented_amount_fixed_on_retry():
    b = bot([_msg(calls=[("get_property", '{"home": "100"}')]),
             _msg("It costs £1,234."), _msg("About £400 to £950.")])  # fmt: skip
    r = b.ask("Cost?")
    assert r.grounded and "£400 to £950" in r.text and UNCHECKED_NOTE not in r.text


def test_legal_conclusion_gets_retry():
    # Regression: Qwen wrote "Home 4 is not exempt now" despite the prompt rule.
    b = bot([LOOKUP, _msg("Flat A is not exempt now."), _msg("Flat A is likely over the cap.")])
    r = b.ask("Is Flat A exempt?")
    assert "exempt now" not in r.text and "likely over the cap" in r.text
    assert "legal conclusions" in b.client.seen[-1][-1]["content"]


def test_legal_conclusion_dropped_if_retry_repeats_it():
    b = bot([LOOKUP, _msg("Flat A is not exempt."), _msg("Flat A isn't exempt. Get quotes first.")])
    r = b.ask("Is Flat A exempt?")
    assert "exempt." not in r.text and r.text.startswith("Get quotes first.")


def test_legal_patterns():
    assert legal_conclusions("Home 4 is not currently exempt.") == ["not currently exempt"]
    assert legal_conclusions("You may be able to register an exemption.") == []
    assert legal_conclusions("You must act.") == ["must"]
    text = "Home 4 is not exempt. Get quotes.\n\nYou must act now."
    assert drop_sentences(text, LEGAL_PATTERNS) == "Get quotes."


def test_tool_talk_gets_one_retry():
    # Regression: Qwen kept writing "The tool states: ..." to landlords.
    b = bot([LOOKUP, _msg("The tool states: get quotes."), _msg("Get quotes for Flat A.")])
    r = b.ask("What next?")
    assert r.text.startswith("Get quotes for Flat A.")
    assert "Don't mention the tool" in b.client.seen[-1][-1]["content"]


def test_tool_talk_lead_in_cut_if_retry_repeats_it():
    # Regression: Qwen kept "The tool states: ..." even after the retry.
    b = bot([LOOKUP, _msg("The data says get quotes."), _msg("The data says get quotes.")])
    assert b.ask("?").text.startswith("Get quotes.")


def test_strip_tool_talk_keeps_the_facts():
    from agent.assistant import strip_tool_talk

    text = 'House A is likely over the cap. The tool states: "You may be able to register."'
    assert strip_tool_talk(text) == 'House A is likely over the cap. "You may be able to register."'
    assert strip_tool_talk("Get quotes first.") == "Get quotes first."


def test_draft_tool_blocked_unless_landlord_asks():
    # Regression: Qwen drafted a quote request for "How much would a heat pump cost?".
    call = ("draft_quote_request", '{"home": "Flat A", "trade": "insulation"}')
    b = bot([_msg(calls=[call]), _msg("Heat pumps aren't on Flat A's route.")])
    r = b.ask("How much would a heat pump cost for Flat A?")
    assert r.tool_calls[0]["result"]["error"].startswith("The landlord didn't ask")
    b = bot([_msg(calls=[call]), _msg("Your quote request is below.")])
    r = b.ask("Write me a quote request for Flat A")
    assert "body" in r.tool_calls[0]["result"]


def test_draft_claim_without_a_draft_gets_retry_then_dropped():
    # Gap 1 ("did the agent do what it said?"): no draft this turn, so "it's below" is false.
    b = bot([LOOKUP, _msg("Flat A needs loft insulation. Your quote request is ready below."),
             _msg("Flat A needs loft insulation. The message is below.")])  # fmt: skip
    r = b.ask("What does Flat A need?")
    assert "no tool result behind them" in b.client.seen[-1][-1]["content"]
    assert r.text.startswith("Flat A needs loft insulation.") and "below" not in r.text


def test_draft_claim_allowed_when_draft_succeeded():
    call = ("draft_quote_request", '{"home": "Flat A", "trade": "insulation"}')
    b = bot([_msg(calls=[call]), _msg("Your quote request is below.")])
    r = b.ask("Write me a quote request for Flat A")
    assert "Your quote request is below." in r.text and len(b.client.seen) == 2  # no retry


def test_send_claims_never_allowed():
    from agent.assistant import unbacked_claims

    done = [{"name": "draft_quote_request", "result": {"body": "x"}}]
    assert unbacked_claims("I've sent it to the electrician.", done) == ["I've sent"]
    assert unbacked_claims("It has been emailed.", done) == ["has been emailed"]
    # Talking about ratings "below C" is not a claim.
    assert unbacked_claims("Flat A's rating is below C. It's below the cap.", []) == []


def test_lead_break_puts_the_takeaway_on_its_own_line():
    from agent.assistant import lead_break

    text = (
        "The total is about £16,285 to £25,100. This covers the cheapest route for each home."
        "\n\nIt leaves out homes already at C."
    )
    assert lead_break(text) == (
        "The total is about £16,285 to £25,100.\n\nThis covers the cheapest route for each "
        "home.\n\nIt leaves out homes already at C."
    )
    assert lead_break("Get quotes first.") == "Get quotes first."
    assert lead_break("Short.\n\nAlready split.") == "Short.\n\nAlready split."


def test_answer_without_tools_gets_one_nudge():
    # Regression: mid-conversation, Qwen said "I don't know the cost for Flat C" without
    # calling a tool.
    b = bot([_msg("I don't know the cost for Flat A."),
             _msg(calls=[("get_property", '{"home": "Flat A"}')]),
             _msg("Flat A would cost about £400 to £950.")])  # fmt: skip
    r = b.ask("How much is Flat A?")
    assert [c["name"] for c in r.tool_calls] == ["get_property"]
    assert r.text.startswith("Flat A would cost about £400 to £950.")


def test_no_tool_needed_keeps_the_second_answer():
    b = bot([_msg("You're welcome."), _msg("You're welcome!")])
    assert b.ask("Thanks!").text.startswith("You're welcome!")


def test_long_sentence_gets_one_retry():
    # Regression: Qwen kept writing 27-29 word sentences, over the style guide's 25.
    long = " ".join(["word"] * 28) + "."
    b = bot([LOOKUP, _msg(long), _msg("Flat A is a fix-early home. Get quotes first.")])
    r = b.ask("Tell me about Flat A")
    assert "25 words or fewer" in b.client.seen[-1][-1]["content"]
    assert r.text.startswith("Flat A is a fix-early home.")


def test_endless_tool_calls_fall_back():
    b = bot([_msg(calls=[("get_rules", "{}")])] * 10)
    r = b.ask("Rules?")
    assert not r.grounded and r.text.startswith(FALLBACK)


def test_bad_tool_arguments_reported_to_model():
    b = bot([_msg(calls=[("get_property", "not json")]), _msg("I couldn't find that home.")])
    r = b.ask("?")
    assert r.tool_calls[0]["result"] == {"error": "arguments must be a JSON object"}


def test_missing_model_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("NEBIUS_MODEL", raising=False)
    with pytest.raises(SystemExit, match="NEBIUS_MODEL"):
        Assistant(Toolbox([], POLICY), client=FakeClient([]))
