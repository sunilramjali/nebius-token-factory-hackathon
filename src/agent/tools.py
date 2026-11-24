"""Tools the assistant may call. Every number comes from the engine (analytics.route_results /
route_steps) or config/policy.yaml; every sentence comes from the report module, so it already
follows docs/report_style.md.

Privacy (docs/data_access.md §3): tool output is sent to Nebius, so it never contains register
address or postcode fields. Homes are identified by UPRN and the landlord's own label only, and
only homes in the landlord's portfolio can be looked up.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

from agent.help import ASSESSOR, TRADES, draft_quote_request, find_help
from engine.policy import Policy
from report.portfolio_report import (
    CONFIDENCE_TEXT,
    Property,
    Step,
    _caveats,
    _totals,
    _within_cap,
    cost_range,
    estimated_spend,
    load_properties,
    next_actions,
    rules_in_brief,
    status,
)

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_summary",
            "description": (
                "List every home in the landlord's portfolio with its UPRN, label, current "
                "rating, status and cheapest cost to reach C, plus portfolio totals and the "
                "next actions. Call this first for any question about 'my homes'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_property",
            "description": (
                "Full engine result for one home in the portfolio: rating, gap to C, the "
                "cheapest route measure by measure, cost range, cap and fix-early flags, "
                "safer and boiler routes, and caveats."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "home": {
                        "type": "string",
                        "description": "The home's label (e.g. 'Home 2') or its UPRN.",
                    }
                },
                "required": ["home"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_help",
            "description": (
                "Who the landlord should contact for one home (assessor, installers, "
                "engineers), in work order, with the official register to find them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "home": {"type": "string", "description": "The home's label or UPRN."}
                },
                "required": ["home"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_quote_request",
            "description": (
                "Write a quote request the landlord can copy and send to one trade for one "
                "home. Use a trade from find_help. The app shows the full message to the "
                "landlord, so don't repeat it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "home": {"type": "string", "description": "The home's label or UPRN."},
                    "trade": {"type": "string", "enum": [*TRADES, ASSESSOR.key]},
                },
                "required": ["home", "trade"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rules",
            "description": (
                "The proposed rules (deadlines, cost cap, fix-early rule) from the policy "
                "config. Proposals only, not law."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _date(d) -> str:
    return f"{d:%-d %B %Y}"


def _steps(steps: list[Step]) -> list[dict[str, Any]]:
    return [
        {
            "measure": s.summary,
            "rating_gain": s.uplift,
            "cost": cost_range(s.cost_low, s.cost_high),
            "rating_after": s.cumulative_score,
            "band_after": s.cumulative_band,
            "counts_toward_cap": s.counts_toward_cap,
        }
        for s in steps
    ]


def _route(steps: list[Step], policy: Policy) -> dict[str, Any] | None:
    if not steps:
        return None
    lo, hi, clo, chi = _totals(steps)
    return {
        "total_cost": cost_range(lo, hi),
        "counts_toward_cap": cost_range(clo, chi),
        "likely_within_cap": _within_cap(steps, policy),
        "rating_after": steps[-1].cumulative_score,
        "measures": _steps(steps),
    }


def property_facts(p: Property, policy: Policy) -> dict[str, Any]:
    """Everything the assistant may say about one home. No address fields."""
    st = status(p, policy)
    below = p.current_score < policy.band_c_threshold
    spend = estimated_spend(p, policy)
    facts: dict[str, Any] = {
        "uprn": p.property_id,
        "label": p.label,
        "property_type": p.property_type,
        "certificate_date": _date(p.registration_date),
        "current_rating": p.current_score,
        "current_band": p.current_band,
        "gap_to_c": max(0, policy.band_c_threshold - p.current_score),
        "status": st.label,
        "next_action": next_actions([p], policy)[0],
        "data_quality": CONFIDENCE_TEXT.get(p.confidence, p.confidence),
    }
    if not below:
        return facts
    facts |= {
        "cheapest_route": {
            "total_cost": cost_range(p.cost_low, p.cost_high) if p.cost_known else None,
            "counts_toward_cap": cost_range(p.cap_low, p.cap_high) if p.cost_known else None,
            "rating_after": p.final_score,
            "reaches_c": p.reaches_c,
            "likely_within_cap": p.within_cap,
            "measures": _steps(p.steps),
        },
        "estimated_spend_stopping_at_cap": cost_range(*spend) if spend else None,
        "fix_early_candidate": p.lock_in_candidate,
        "likely_over_cap": p.likely_exempt_cost_cap,
        "borderline": p.borderline,
        "cost_range_spans_cap": p.cap_verdict_uncertain,
        "needs_boiler_to_stay_within_cap": p.lock_in_needs_boiler,
        "safer_route": _route(p.safer, policy),
        "boiler_route": _route(p.alternative, policy),
        "caveats": _caveats(p, policy),
        "exemption": exemption_text(st.key, policy),
    }
    return facts


def exemption_text(status_key: str, policy: Policy) -> str | None:
    """Exemption wording, as in the report's verdict(). None when no exemption is in play, so
    the model has nothing to embellish (a calibration run invented a qualifying condition)."""
    cap = f"£{policy.cost_cap_gbp:,}"
    if status_key == "over_cap":
        return (
            f"You may be able to register an exemption after spending up to {cap}. "
            "The exemption process for the 2030 rules isn't published yet."
        )
    if status_key == "cannot":
        return (
            f"After spending up to {cap}, you may be able to register an exemption. "
            "A survey may find other measures."
        )
    return None


def portfolio_summary(props: list[Property], policy: Policy) -> dict[str, Any]:
    homes = []
    for p in props:
        st = status(p, policy)
        homes.append(
            {
                "uprn": p.property_id,
                "label": p.label,
                "current_band": p.current_band,
                "current_rating": p.current_score,
                "gap_to_c": max(0, policy.band_c_threshold - p.current_score),
                "status": st.label,
                # None when there's no costed route to C (done, survey, can't reach C).
                "cheapest_cost_to_c": (
                    cost_range(p.cost_low, p.cost_high)
                    if st.key not in {"done", "survey", "cannot"}
                    else None
                ),
            }
        )
    spends = [x for x in (estimated_spend(p, policy) for p in props) if x is not None]
    # Counts follow the status, so no home is counted in two groups (likely_exempt_cost_cap
    # covers both "over the cap" and "can't reach C", which a model double-counted).
    keys = [status(p, policy).key for p in props]
    return {
        "homes": homes,
        "homes_below_c": sum(p.current_score < policy.band_c_threshold for p in props),
        "fix_early": sum(k in {"fix", "borderline", "quotes", "boiler"} for k in keys),
        "likely_over_cap": keys.count("over_cap"),
        "cant_reach_c": keys.count("cannot"),
        "survey_first": keys.count("survey"),
        "already_c": keys.count("done"),
        "estimated_total_spend": (
            cost_range(sum(x[0] for x in spends), sum(x[1] for x in spends)) if spends else None
        ),
        # Says what the total covers, so the model doesn't describe it wrongly.
        "estimated_total_spend_covers": (
            "Spend if you follow each home's cheapest route, stopping at the "
            f"£{policy.cost_cap_gbp:,} cap per home. It leaves out homes already at C, homes "
            "that can't reach C and homes that need a survey first."
        ),
        "next_actions": next_actions(props, policy),
    }


def rules(policy: Policy) -> dict[str, Any]:
    return {
        "status": "Proposals from the government response of January 2026. Not yet law.",
        "summary": re.sub(r"<[^>]+>", "", rules_in_brief(policy)),
        "compliance_deadline": _date(policy.compliance_deadline),
        "fix_early_deadline": _date(policy.lock_in_deadline),
        "cost_cap": f"£{policy.cost_cap_gbp:,}",
        "lower_cap_below_property_value": f"£{policy.low_value_threshold_gbp:,}",
        "lower_cap_note": (
            f"Homes worth under £{policy.low_value_threshold_gbp:,} would have a lower cap. "
            "This is not an exemption."
        ),
        "certificate_valid_years": policy.epc_validity_years,
    }


class Toolbox:
    """Runs tool calls against one landlord's portfolio."""

    def __init__(self, props: list[Property], policy: Policy, missing: list[str] | None = None):
        self.policy = policy
        self.props = {p.property_id: p for p in props}
        self.missing = missing or []  # UPRNs with no engine result (shown in the app)

    @classmethod
    def from_db(
        cls, con: duckdb.DuckDBPyConnection, labels: dict[int, str], policy: Policy
    ) -> Toolbox:
        """`labels`: UPRN -> the landlord's own name for the home (never a register address)."""
        ids = [str(u) for u in labels]
        props, missing = load_properties(
            con, ids, {str(u): lab for u, lab in labels.items()}, None, strict=False
        )
        return cls(props, policy, missing)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_portfolio_summary":
            return portfolio_summary(list(self.props.values()), self.policy)
        if name == "get_rules":
            return rules(self.policy)
        if name in ("get_property", "find_help", "draft_quote_request"):
            # Models pass the label as often as the UPRN; accept either ("uprn" for old calls).
            key = str(args.get("home") or args.get("uprn") or "").strip()
            p = self.props.get(key) or next(
                (p for p in self.props.values() if p.label.casefold() == key.casefold()), None
            )
            if p is None:
                return {
                    "error": f"{key!r} isn't in this portfolio.",
                    "homes": [p.label for p in self.props.values()],
                }
            if name == "find_help":
                return find_help(p, self.policy)
            if name == "draft_quote_request":
                return draft_quote_request(p, self.policy, str(args.get("trade", "")))
            return property_facts(p, self.policy)
        return {"error": f"unknown tool {name!r}"}
