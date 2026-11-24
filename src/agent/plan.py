"""One plan for the whole portfolio (or one home), built by code: the autonomous step.

For every home that needs work, in priority order: its status, cheapest cost, next action,
who to contact (in work order) and how many quote requests are ready. The agent only
summarises it. The messages themselves are rebuilt by code in the app (draft_quote_request),
so the model never sees or rewrites them, and nothing is sent: the landlord approves each one.
"""

from __future__ import annotations

from typing import Any

from agent.help import find_help
from engine.policy import Policy
from report.portfolio_report import ACTIONS, Property, cost_range, next_actions, status


def _priority(p: Property, policy: Policy) -> tuple:
    key = status(p, policy).key
    return (ACTIONS.get(key, (9,))[0], p.cost_mid or 0, p.label)


def make_plan(props: list[Property], policy: Policy) -> dict[str, Any]:
    """Plan for the given homes. Homes already at C are counted, not planned."""
    todo = [p for p in props if p.current_score < policy.band_c_threshold]
    homes = []
    for p in sorted(todo, key=lambda p: _priority(p, policy)):
        st = status(p, policy)
        steps = [
            {k: n[k] for k in ("trade", "who", "for", "check", "find_at", "link")}
            for n in find_help(p, policy)["needed"]
        ]
        homes.append(
            {
                "home": p.label,
                "uprn": p.property_id,
                "status": st.label,
                "cheapest_cost": (
                    cost_range(p.cost_low, p.cost_high)
                    if p.cost_known and st.key not in {"survey", "cannot"}
                    else None
                ),
                "next_action": next_actions([p], policy)[0],
                "contacts_in_order": steps,
                "messages_ready": len(steps),
            }
        )
    return {
        "homes_planned": len(homes),
        "already_c": len(props) - len(todo),
        "messages_ready": sum(h["messages_ready"] for h in homes),
        "start_with": homes[0]["home"] if homes else None,
        "homes": homes,
        "note": "The app shows each message below the reply. Nothing is sent: the landlord "
        "approves and sends each one.",
    }
