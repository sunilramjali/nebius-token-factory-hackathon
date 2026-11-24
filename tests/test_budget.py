import json

from test_agent import LOOKUP, POLICY, FakeClient, _msg, prop

from agent.assistant import BUDGET_UNCHECKED, Assistant
from agent.budget import best_possible, budget_options, home_options
from agent.tools import Toolbox
from report.portfolio_report import Step


def steps(*costs):
    """A route whose rating climbs from 60 by 5 a measure; C (69) needs 2 measures."""
    out, rating = [], 60
    for i, (lo, hi) in enumerate(costs, start=1):
        rating += 5
        out.append(Step(i, f"Measure {i}", 5, lo, hi, rating, "C" if rating >= 69 else "D"))
    return out


def homes():
    a = prop("1", "Flat A", steps=steps((400, 600), (400, 600)), lo=800, hi=1200)  # mid 1,000
    b = prop("2", "Flat B", steps=steps((1000, 2000), (1000, 2000)), lo=2000, hi=4000)  # 3,000
    c = prop("3", "Flat C", steps=steps((3000, 5000), (3000, 5000)), lo=6000, hi=10000)  # 8,000
    done = prop("4", "House B", score=72, lock=False)
    return [a, b, c, done]


AB = [{"home": "flat a", "option": "cheapest"}, {"home": "Flat B", "option": "cheapest"}]
SAME = "Fix Flat A and Flat B."


def tb():
    return Toolbox(homes(), POLICY)


def test_options_cover_full_route_and_partials_but_not_homes_at_c():
    opts = home_options(homes()[0], POLICY)
    assert [o["option"] for o in opts] == ["cheapest", "first_1"]
    assert (
        opts[0]["reaches_c"] and opts[0]["cost_mid"] == 1000 and opts[0]["cost"] == "£800 to £1,200"
    )
    assert not opts[1]["reaches_c"] and opts[1]["rating_after"] == 65
    assert home_options(homes()[3], POLICY) == []
    assert "House B" not in json.dumps(budget_options(homes(), POLICY))


def test_best_possible_maximises_homes_reaching_c_within_budget():
    assert best_possible(homes(), POLICY, 4000)["homes_reaching_c"] == 2  # A + B
    assert best_possible(homes(), POLICY, 999)["homes_reaching_c"] == 0
    assert best_possible(homes(), POLICY, 12000)["homes_reaching_c"] == 3


def test_check_passes_a_plan_within_budget():
    out = tb().call("check_budget_plan", {"budget": "£4,000", "choices": AB})
    assert out["passed"] and out["homes_reaching_c"] == 2 == out["best_possible_homes_reaching_c"]
    assert out["total_cost"] == "£2,800 to £5,200" and out["left_over_mid"] == "£0"
    assert any("top of the cost range" in w for w in out["warnings"])


def test_check_fails_over_budget_duplicates_and_unknown_options():
    out = tb().call("check_budget_plan", {"budget": 4000, "choices": [
        {"home": "Flat A", "option": "cheapest"}, {"home": "Flat C", "option": "cheapest"},
        {"home": "Flat A", "option": "first_1"}, {"home": "Flat B", "option": "gold"},
        {"home": "Flat Z", "option": "cheapest"}]})  # fmt: skip
    assert not out["passed"]
    errors = " | ".join(out["errors"])
    assert "over budget by about £5,000" in errors and "in the plan twice" in errors
    assert "no option 'gold'" in errors and "'Flat Z' isn't in this portfolio" in errors


def test_check_rejects_bad_budgets_and_empty_plans():
    assert not tb().call("check_budget_plan", {"budget": "lots", "choices": []})["passed"]
    assert not tb().call("check_budget_plan", {"budget": 0, "choices": []})["passed"]
    out = tb().call("check_budget_plan", {"budget": 5000, "choices": []})
    assert out["errors"] == ["the plan has no homes in it"]


def test_unchecked_budget_plan_gets_retry_then_a_note():
    opts = _msg(calls=[("get_budget_options", "{}")])
    b = Assistant(tb(), client=FakeClient(
        [opts, _msg(SAME), _msg(SAME)]), model="t")  # fmt: skip
    r = b.ask("I have £4,000. What should I do?")
    assert "hasn't passed check_budget_plan" in b.client.seen[-1][-1]["content"]
    assert BUDGET_UNCHECKED in r.text


def test_checked_budget_plan_needs_no_note():
    check = ("check_budget_plan", json.dumps({"budget": 4000, "choices": AB}))
    b = Assistant(tb(), client=FakeClient([
        _msg(calls=[("get_budget_options", "{}")]), _msg(calls=[check]),
        _msg("Fix Flat A and Flat B. Both would reach C.")]), model="t")  # fmt: skip
    r = b.ask("I have £4,000. What should I do?")
    assert BUDGET_UNCHECKED not in r.text and r.tool_calls[-1]["result"]["passed"]
    assert LOOKUP  # shared fake-tool helper stays importable


def test_partial_option_only_messages_the_funded_trades():
    from agent.budget import plan_for_choices
    from agent.plan import make_plan

    h = homes()
    h[2].steps = [Step(1, "Heating controls", 5, 300, 400, 65, "D", True, "G"),
                  Step(2, "Wall insulation", 5, 3000, 5000, 70, "C", True, "Q")]  # fmt: skip
    tb_ = Toolbox(h, POLICY)
    chosen = [{"uprn": "3", "option": "first_1"}]
    plan = plan_for_choices(make_plan([h[2]], POLICY), chosen, tb_.home)
    assert [n["trade"] for n in plan["homes"][0]["contacts_in_order"]] == ["heating"]
    assert plan["messages_ready"] == 1  # no insulation installer, no assessor yet


def test_option_labels_use_singular_for_one_measure():
    one = prop("9", "Flat Z", steps=steps((400, 600), (400, 600))[1:], lo=400, hi=600)
    one.steps[0] = Step(1, "Measure 1", 10, 400, 600, 70, "C")
    assert home_options(one, POLICY)[0]["label"] == "Cheapest route to C (1 measure)"
