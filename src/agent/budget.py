"""Plan to a budget: the agent decides, code checks.

get_budget_options lists, per home, what the money could buy (from the engine's routes):
the cheapest route to C, a safer route, a boiler route, or just the first few measures.
The agent picks options for the landlord's budget, then must call check_budget_plan, which
adds up the cost, compares it with the budget and the cap, and says the best result that was
possible. The agent revises until the check passes; the app shows only a checked plan.
No number here is the model's: every cost and rating comes from the engine.
"""

from __future__ import annotations

from itertools import product
from typing import Any

from engine.policy import Policy
from report.portfolio_report import Property, Step, cost_range, status


def _mid(steps: list[Step]) -> float:
    return sum(((s.cost_low or 0) + (s.cost_high or 0)) / 2 for s in steps)


def _option(key: str, label: str, steps: list[Step], policy: Policy) -> dict[str, Any]:
    lo = sum(s.cost_low or 0 for s in steps)
    hi = sum(s.cost_high or 0 for s in steps)
    cap_mid = _mid([s for s in steps if s.counts_toward_cap])
    rating = steps[-1].cumulative_score
    return {
        "option": key,
        "label": label,
        "measures": [s.summary for s in steps],
        "cost": cost_range(lo, hi),
        "cost_low": lo,
        "cost_high": hi,
        "cost_mid": round(_mid(steps)),
        "rating_after": rating,
        "reaches_c": rating >= policy.band_c_threshold,
        "likely_within_cap": cap_mid <= policy.cost_cap_gbp,
    }


def home_options(p: Property, policy: Policy) -> list[dict[str, Any]]:
    """What money could buy for one home. Empty if it's at C or costs aren't known."""
    if p.current_score >= policy.band_c_threshold or not p.cost_known or not p.steps:
        return []
    n = len(p.steps)
    opts = []
    if p.reaches_c:
        word = "measure" if n == 1 else "measures"
        opts.append(_option("cheapest", f"Cheapest route to C ({n} {word})", p.steps, policy))
    if p.safer:
        opts.append(_option("safer", "Safer route, a little above C", p.safer, policy))
    if p.alternative:
        opts.append(_option("boiler", "Route with a new gas or oil boiler", p.alternative, policy))
    for k in range(1, n if p.reaches_c else n + 1):  # partial: a rating gain, not C
        label = "Just the first measure" if k == 1 else f"Just the first {k} measures"
        opts.append(_option(f"first_{k}", label, p.steps[:k], policy))
    return opts


def budget_options(props: list[Property], policy: Policy) -> dict[str, Any]:
    homes = []
    for p in props:
        opts = home_options(p, policy)
        if opts:
            homes.append(
                {
                    "home": p.label,
                    "status": status(p, policy).label,
                    "current_rating": p.current_score,
                    "options": opts,
                }  # fmt: skip
            )
    return {
        "homes": homes,
        "how_to_use": "Pick at most one option per home, then call check_budget_plan with the "
        "landlord's budget. Revise until it passes.",
    }


def best_possible(props: list[Property], policy: Policy, budget: float) -> dict[str, Any]:
    """Most homes reaching C within the budget (middle of the cost range), cheapest first.
    Exhaustive: a portfolio has a handful of homes with a few options each."""
    choices = [[None, *home_options(p, policy)] for p in props]
    best = (0, 0.0)
    for combo in product(*choices):
        cost = sum(o["cost_mid"] for o in combo if o)
        if cost > budget:
            continue
        reach = sum(1 for o in combo if o and o["reaches_c"])
        if reach > best[0] or (reach == best[0] and reach and cost < best[1]):
            best = (reach, cost)
    return {"homes_reaching_c": best[0], "cost_mid": round(best[1])}


def check_budget_plan(
    props_by_label: dict[str, Property],
    find_home,
    policy: Policy,
    budget: Any,
    choices: Any,
) -> dict[str, Any]:
    """Validate the agent's plan. `find_home` resolves a label or UPRN to a Property."""
    errors: list[str] = []
    try:
        budget = float(str(budget).replace("£", "").replace(",", ""))
    except ValueError:
        return {"passed": False, "errors": [f"budget {budget!r} isn't a number"]}
    if budget <= 0:
        return {"passed": False, "errors": ["the budget must be more than £0"]}

    chosen, seen = [], set()
    for c in choices if isinstance(choices, list) else []:
        c = c if isinstance(c, dict) else {}
        p = find_home(c.get("home", ""))
        if p is None:
            errors.append(f"{c.get('home')!r} isn't in this portfolio")
            continue
        if p.property_id in seen:
            errors.append(f"{p.label} is in the plan twice")
            continue
        opt = next((o for o in home_options(p, policy) if o["option"] == c.get("option")), None)
        if opt is None:
            errors.append(f"{p.label} has no option {c.get('option')!r}")
            continue
        seen.add(p.property_id)
        chosen.append({"home": p.label, "uprn": p.property_id, **opt})
    if not chosen and not errors:
        errors.append("the plan has no homes in it")

    lo = sum(o["cost_low"] for o in chosen)
    hi = sum(o["cost_high"] for o in chosen)
    mid = sum(o["cost_mid"] for o in chosen)
    over = max(0, round(mid - budget))
    warnings = [f"{o['home']}: this option is likely over the cap" for o in chosen
                if not o["likely_within_cap"]]  # fmt: skip
    if hi > budget >= mid:
        warnings.append("The top of the cost range is over budget. Real quotes will decide.")
    best = best_possible(list(props_by_label.values()), policy, budget)
    reach = sum(1 for o in chosen if o["reaches_c"])
    if over:
        errors.append(f"over budget by about £{over:,} (middle of the cost range)")
    return {
        "passed": not errors,
        "budget": f"£{budget:,.0f}",
        "total_cost": cost_range(lo, hi) if chosen else None,
        "total_cost_mid": f"£{mid:,.0f}",
        "left_over_mid": f"£{max(0, budget - mid):,.0f}",
        "homes_reaching_c": reach,
        "best_possible_homes_reaching_c": best["homes_reaching_c"],
        "plan": [
            {
                k: o[k]
                for k in (
                    "home",
                    "uprn",
                    "option",
                    "label",
                    "measures",
                    "cost",
                    "rating_after",
                    "reaches_c",
                )
            }  # fmt: skip
            for o in chosen
        ],
        "errors": errors,
        "warnings": warnings,
    }


def plan_for_choices(plan: dict[str, Any], choices: list[dict[str, Any]], props) -> dict[str, Any]:
    """Trim a make_plan result to what a budget plan funds: a home on a partial option
    ("first_k") only gets messages for the trades doing those k measures, and no assessor yet
    (it won't reach C)."""
    from agent.help import trade_for

    by_uprn = {c["uprn"]: c for c in choices}
    homes = []
    for h in plan["homes"]:
        c = by_uprn.get(h["uprn"])
        if c and c["option"].startswith("first_"):
            p = props(h["uprn"])
            k = int(c["option"].split("_")[1])
            trades = {trade_for(s).key for s in p.steps[:k]}
            h = {**h, "contacts_in_order": [n for n in h["contacts_in_order"]
                                            if n["trade"] in trades]}  # fmt: skip
            h["messages_ready"] = len(h["contacts_in_order"])
        homes.append(h)
    return {**plan, "homes": homes, "messages_ready": sum(h["messages_ready"] for h in homes)}
