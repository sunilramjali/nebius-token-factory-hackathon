"""Who to contact for each home, and a quote request the landlord can send. Code only.

The agent never picks a trade or writes the request itself: it calls find_help /
draft_quote_request and explains the result. Links go to official, neutral registers only
(no named firms, no referral fees: docs/data_access.md §3 and docs/report_style.md).
Every link below was checked to load (2026-11-24). Schemes whose sites couldn't be checked
are named in words, with TrustMark as the link.

Nothing here is sent anywhere. The landlord copies the draft and sends it themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.policy import Policy
from report.portfolio_report import Property, Step, cost_range


@dataclass(frozen=True)
class Register:
    name: str
    url: str


GOV_EPC = Register(
    "GOV.UK: get a new energy certificate", "https://www.gov.uk/get-new-energy-certificate"
)
TRUSTMARK = Register(
    "TrustMark: find a tradesperson", "https://www.trustmark.org.uk/find-a-tradesperson"
)
MCS = Register("MCS: find a certified installer", "https://mcscertified.com/find-an-installer/")


@dataclass(frozen=True)
class Trade:
    key: str
    who: str  # plain words for the landlord
    check: str  # what to ask them for
    register: Register


TRADES = {
    "insulation": Trade(
        "insulation", "an insulation installer", "Ask for their TrustMark registration.", TRUSTMARK
    ),
    "gas": Trade(
        "gas", "a Gas Safe registered engineer", "Ask to see their Gas Safe ID card.", TRUSTMARK
    ),
    "renewables": Trade(
        "renewables", "an MCS certified installer", "Ask for their MCS certificate number.", MCS
    ),
    "electrical": Trade(
        "electrical",
        "a registered electrician",
        "Ask which competent person scheme they belong to.",
        TRUSTMARK,
    ),
    "windows": Trade(
        "windows",
        "a window and door installer",
        "Ask if they're FENSA or CERTASS registered.",
        TRUSTMARK,
    ),
    "heating": Trade(
        "heating",
        "a heating engineer or plumber",
        "Ask for their TrustMark registration.",
        TRUSTMARK,
    ),
    "general": Trade(
        "general", "a retrofit coordinator", "They can plan and check the whole job.", TRUSTMARK
    ),
}
ASSESSOR = Trade(
    "assessor",
    "an accredited energy assessor",
    "They'll check the home and issue a new EPC.",
    GOV_EPC,
)

# EPC improvement codes (the certificate's own measure types) -> trade.
TRADE_BY_CODE = {
    **dict.fromkeys(["A", "A2", "A3", "B", "B4", "Q", "W", "W1", "W2"], "insulation"),
    **dict.fromkeys(["I", "S", "T", "T2"], "gas"),
    **dict.fromkeys(["U", "U1", "N", "Z1", "V"], "renewables"),
    **dict.fromkeys(["L", "L2", "E", "M"], "electrical"),
    **dict.fromkeys(["O", "O3", "P", "D", "X"], "windows"),
    **dict.fromkeys(["G", "H", "C", "F", "Y"], "heating"),
}
COORDINATOR_FROM_STEPS = 3  # several trades to line up: suggest someone to plan it


def trade_for(step: Step) -> Trade:
    return TRADES[TRADE_BY_CODE.get(step.improvement_type or "", "general")]


def route_for_help(p: Property) -> list[Step]:
    """The route the landlord would follow: the boiler route when it's the one within the cap."""
    return p.alternative if p.lock_in_needs_boiler and p.alternative else p.steps


def find_help(p: Property, policy: Policy) -> dict:
    """Who to contact for this home, in the order the work would happen."""
    if p.current_score >= policy.band_c_threshold:
        return {"home": p.label, "needed": [], "note": "No works needed now."}
    steps = route_for_help(p)
    groups: dict[str, list[Step]] = {}
    for s in steps:
        groups.setdefault(trade_for(s).key, []).append(s)
    needed = [_entry(TRADES[k], ss) for k, ss in groups.items()]
    if len(steps) >= COORDINATOR_FROM_STEPS or p.borderline:
        needed.insert(0, _entry(TRADES["general"], []))
    if p.reaches_c:  # a new EPC is what records the C
        needed.append(_entry(ASSESSOR, []))
    else:
        needed.insert(0, _entry(ASSESSOR, []))  # a survey may find other measures
    return {"home": p.label, "needed": needed}


def _entry(trade: Trade, steps: list[Step]) -> dict:
    return {
        "trade": trade.key,
        "who": trade.who,
        "for": [s.summary for s in steps],
        "check": trade.check,
        "find_at": trade.register.name,
        "link": trade.register.url,
    }


def draft_quote_request(p: Property, policy: Policy, trade_key: str) -> dict:
    """A short message asking one trade for a quote. Built from engine output only."""
    steps = route_for_help(p)
    if trade_key == ASSESSOR.key:
        trade, mine = ASSESSOR, []
    elif trade_key in TRADES:
        trade = TRADES[trade_key]
        mine = [s for s in steps if trade_for(s).key == trade_key]
    else:
        return {"error": f"unknown trade {trade_key!r}", "trades": [*TRADES, ASSESSOR.key]}

    target = policy.band_c_threshold
    lines = [
        "Hello,",
        "",
        f"I'm a landlord looking for a quote for work on a rented {p.property_type or 'home'}.",
        "Address: [add the address]",
        "",
    ]
    if trade is ASSESSOR:
        lines += [
            "Please could you quote for a new EPC assessment.",
            f"The current rating is {p.current_score} ({p.current_band}). "
            f"I'm aiming for C ({target} or above).",
        ]
    else:
        lines.append("The EPC suggests this work:")
        lines += [
            f"- {s.summary} (indicative cost {cost_range(s.cost_low, s.cost_high)})" for s in mine
        ]
        lines += [
            "",
            f"The current rating is {p.current_score} ({p.current_band}). "
            f"I'm aiming for C ({target} or above).",
            "These costs are only estimates from the EPC register. A survey is needed first.",
        ]
    lines += [
        "",
        "Could you let me know your price, when you could start, and your registration details?",
        "",
        "Thank you,",
        "[your name]",
    ]
    return {
        "home": p.label,
        "to": trade.who,
        "find_at": trade.register.name,
        "link": trade.register.url,
        "subject": f"Quote request: {_short(mine) if mine else 'new EPC assessment'}",
        "body": "\n".join(lines),
    }


def _short(steps: list[Step]) -> str:
    first = steps[0].summary
    return first if len(steps) == 1 else f"{first} and {len(steps) - 1} more"
