import json

from test_agent import POLICY, FakeClient, _msg, prop

from agent.assistant import Assistant, unbacked_claims, wants_draft
from agent.plan import make_plan
from agent.tools import Toolbox


def homes():
    fix = prop("1", "Flat A", lo=400, hi=950)  # fix early, cheap
    pricey_fix = prop("2", "Flat B", lo=3000, hi=5000)  # fix early, dearer
    over = prop("3", "House A", lock=False, exempt=True, lo=9000, hi=20000)
    done = prop("4", "House B", score=72, lock=False)
    return [over, done, pricey_fix, fix]


def test_plan_orders_by_priority_then_cost_and_skips_homes_at_c():
    plan = make_plan(homes(), POLICY)
    assert [h["home"] for h in plan["homes"]] == ["Flat A", "Flat B", "House A"]
    assert plan["already_c"] == 1 and plan["start_with"] == "Flat A"
    assert plan["messages_ready"] == sum(len(h["contacts_in_order"]) for h in plan["homes"])
    assert plan["homes"][0]["contacts_in_order"][-1]["trade"] == "assessor"  # new EPC last


def test_plan_carries_no_message_text_or_addresses():
    # The agent gets the plan; messages are rebuilt by code in the app.
    out = json.dumps(make_plan(homes(), POLICY))
    assert "Hello," not in out and "[add the address]" not in out
    assert "SECRET" not in out and "E13" not in out


def test_make_plan_tool_for_one_home_or_all():
    tb = Toolbox(homes(), POLICY)
    assert [h["home"] for h in tb.call("make_plan", {"home": "flat b"})["homes"]] == ["Flat B"]
    assert tb.call("make_plan", {})["homes_planned"] == 3
    assert tb.call("make_plan", {"home": "all"})["homes_planned"] == 3
    assert "error" in tb.call("make_plan", {"home": "Flat Z"})


def test_plan_requests_open_the_gate():
    for q in ["Make my plan", "Sort out Flat A for me", "Can you do it all for me?",
              "Please handle Flat B", "Sort everything out"]:  # fmt: skip
        assert wants_draft(q), q
    assert not wants_draft("How much would a heat pump cost for Flat A?")


def test_plan_counts_as_evidence_for_messages_ready():
    # Regression: the plural "messages are ready" slipped past the claim check.
    plan = {"name": "make_plan", "result": {"messages_ready": 5}}
    assert unbacked_claims("5 messages are ready below.", [plan]) == []
    empty = {"name": "make_plan", "result": {"messages_ready": 0}}
    assert unbacked_claims("Your messages are ready below.", [empty])


def test_assistant_blocks_unasked_plan():
    call = ("make_plan", "{}")
    b = Assistant(Toolbox(homes(), POLICY), client=FakeClient(
        [_msg(calls=[call]), _msg("Flat A is a fix-early home.")]), model="t")  # fmt: skip
    r = b.ask("How much is Flat A?")
    assert r.tool_calls[0]["result"]["error"].startswith("The landlord didn't ask")


def test_plan_card_escapes_and_lists_contacts():
    from webapp.view import plan_home

    h = make_plan([prop(label="<b>Flat A</b>")], POLICY)["homes"][0]
    out = plan_home(h)
    assert "<b>Flat A</b>" not in out and "&lt;b&gt;Flat A&lt;/b&gt;" in out
    assert "About £400 to £950" in out and "https://www.gov.uk/get-new-energy-certificate" in out
