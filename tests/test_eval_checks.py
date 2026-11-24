"""The eval's checks must be able to fail ("would an agent that always says done pass?")."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from test_agent import POLICY, prop

from agent.tools import Toolbox

_spec = importlib.util.spec_from_file_location(
    "eval_models", Path(__file__).resolve().parents[1] / "scripts" / "eval_models.py"
)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

TB = Toolbox([prop("1", "Flat A"), prop("2", "Flat B")], POLICY)
ITEM = {"expect": ("draft_quote_request", "Flat A", "electrical")}


def reply(*calls):
    return SimpleNamespace(tool_calls=[{"name": n, "args": a} for n, a in calls])


def test_right_tool_home_and_trade_passes():
    r = reply(("draft_quote_request", {"home": "flat a", "trade": "electrical"}))
    assert ev.expectation_met(ITEM, r, TB)


def test_wrong_home_wrong_trade_or_no_call_fails():
    assert not ev.expectation_met(ITEM, reply(("draft_quote_request",
                                  {"home": "Flat B", "trade": "electrical"})), TB)  # fmt: skip
    assert not ev.expectation_met(ITEM, reply(("draft_quote_request",
                                  {"home": "Flat A", "trade": "gas"})), TB)  # fmt: skip
    assert not ev.expectation_met(ITEM, reply(), TB)  # said "done", called nothing
    assert not ev.expectation_met(ITEM, None, TB)


def test_scenarios_start_mid_conversation_and_plant_an_error():
    assert all(s["history"] for s in ev.SCENARIOS)
    planted = next(s for s in ev.SCENARIOS if s["id"] == "s_planted_error")
    assert planted["forbid"] == ["£500"] and "£500" in planted["history"][-1][1]


def test_summary_counts_for_a_home_when_allowed():
    item = {"expect": (("get_property", "get_portfolio_summary"), "Flat B", None)}
    assert ev.expectation_met(item, reply(("get_portfolio_summary", {})), TB)
    assert ev.expectation_met(item, reply(("get_property", {"home": "Flat B"})), TB)
    assert not ev.expectation_met(item, reply(("get_property", {"home": "Flat A"})), TB)
    assert not ev.expectation_met(ITEM, reply(("get_portfolio_summary", {})), TB)  # not allowed


def test_budget_quality_needs_a_passing_and_best_plan():
    item = {"id": "budget"}

    def r(*results):
        return SimpleNamespace(tool_calls=[{"name": "check_budget_plan", "result": x}
                                           for x in results])  # fmt: skip

    best = {"passed": True, "homes_reaching_c": 2, "best_possible_homes_reaching_c": 2}
    weak = {"passed": True, "homes_reaching_c": 1, "best_possible_homes_reaching_c": 2}
    failed = {"passed": False, "homes_reaching_c": 2, "best_possible_homes_reaching_c": 2}
    assert ev.budget_quality(item, r(failed, best))
    assert not ev.budget_quality(item, r(best, weak))  # the last passing plan counts
    assert not ev.budget_quality(item, r(failed)) and not ev.budget_quality(item, None)
    assert ev.budget_quality({"id": "priority"}, None)  # other questions unaffected
