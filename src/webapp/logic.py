"""Pure logic behind app.py: turn typed rows into UPRNs, and engine output into table rows.

Privacy (docs/data_access.md §3): register addresses are shown only for homes the landlord
typed (to settle an ambiguous match) and never reach the assistant. A home with no label gets
"Home N", not its address, because labels are sent to Nebius.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

from agent.tools import Toolbox, property_facts
from engine.policy import Policy
from lookup.address_match import normalise_postcode, split_postcode
from lookup.portfolio import (
    PortfolioRow,
    Resolved,
    load_candidates,
    resolve,
)
from report.portfolio_report import NEEDS_INPUT_MESSAGES

# Landlord-facing text (docs/report_style.md). {cap} and {deadline} come from the policy.
TEXT = {
    "title": "Route to EPC C",
    "intro": (
        "Enter your rented homes in Newham. You'll see each home's rating, the cheapest way to "
        "reach C, and whether it's likely within the {cap} cap."
    ),
    "entry_help": "One home per row. Add the postcode. The label is your own name for the home.",
    "check": "Check my homes",
    "demo": "Try 6 example homes",
    "needs_input": "Some homes need your input",
    "pick": "Which one is it?",
    "results": "Your homes",
    "fix_early_note": "Fix early: a C by {deadline} should count as compliant until it expires.",
    "report": "Download the full report",
    "chat": "Ask about your homes",
    "chat_placeholder": "For example: which home should I fix first?",
    "chat_off": "The assistant is off. Add NEBIUS_API_KEY and NEBIUS_MODEL to .env to turn it on.",
    "chat_error": "The assistant isn't available right now. The table above still works.",
    "working": "How this answer was built",
    "no_homes": "Add at least one home with a postcode.",
}

# One home per situation, high-confidence private rentals, picked deterministically. Used for
# the demo button and by scripts/eval_models.py. Identified by UPRN only: no addresses.
DEMO_PORTFOLIO_SQL = """
WITH base AS (
    SELECT r.* FROM analytics.route_results r
    JOIN marts.fct_property_compliance c USING (property_id)
    WHERE c.is_private_rented AND r.confidence = 'high' AND r.uprn IS NOT NULL
)
SELECT label, uprn FROM (
  (SELECT 'Home 1' AS label, uprn FROM base WHERE lock_in_candidate AND NOT borderline
     AND NOT cap_verdict_uncertain AND NOT lock_in_needs_boiler ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 'Home 2', uprn FROM base WHERE lock_in_candidate AND borderline
     AND NOT lock_in_needs_boiler AND safer_cost_mid IS NOT NULL ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 'Home 3', uprn FROM base WHERE lock_in_candidate AND cap_verdict_uncertain
     AND NOT borderline AND NOT lock_in_needs_boiler ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 'Home 4', uprn FROM base WHERE exempt_reason = 'over_cap' ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 'Home 5', uprn FROM base WHERE exempt_reason = 'cannot_reach_c' ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 'Home 6', uprn FROM base WHERE NOT below_c ORDER BY uprn LIMIT 1)
) ORDER BY label
"""


def text(key: str, policy: Policy) -> str:
    return TEXT[key].format(
        cap=f"£{policy.cost_cap_gbp:,}", deadline=f"{policy.lock_in_deadline:%-d %B %Y}"
    )


def demo_entries(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    return [
        {"label": label, "address": "", "postcode": "", "uprn": str(uprn)}
        for label, uprn in con.execute(DEMO_PORTFOLIO_SQL).fetchall()
    ]


def rows_from_entries(entries: list[dict[str, Any]]) -> list[PortfolioRow]:
    """Editor rows -> PortfolioRow. Blank rows are skipped; a blank label becomes "Home N"."""
    rows = []
    for e in entries:
        address = str(e.get("address") or "").strip()
        uprn = re.sub(r"\D", "", str(e.get("uprn") or ""))
        if not address and not uprn:
            continue
        n = len(rows) + 1
        rows.append(
            PortfolioRow(
                line=n,
                address=address,
                postcode=normalise_postcode(str(e.get("postcode") or "")),
                label=str(e.get("label") or "").strip() or f"Home {n}",
                uprn=int(uprn) if uprn else None,
            )
        )
    return rows


def resolve_rows(con: duckdb.DuckDBPyConnection, rows: list[PortfolioRow]) -> list[Resolved]:
    postcodes = sorted({pc for r in rows if (pc := r.postcode or split_postcode(r.address)[1])})
    return resolve(rows, load_candidates(con, postcodes))


def needs_input_message(status: str) -> str:
    """The report's message for a row that couldn't be matched, without HTML tags."""
    return re.sub(r"<[^>]+>", "", NEEDS_INPUT_MESSAGES.get(status, status))


def build_toolbox(
    con: duckdb.DuckDBPyConnection, resolved: list[Resolved], policy: Policy
) -> Toolbox:
    ok = [r for r in resolved if r.status in ("matched", "given")]
    return Toolbox.from_db(con, {r.uprn: r.row.label for r in ok}, policy)


def table_rows(toolbox: Toolbox) -> list[dict[str, Any]]:
    """One row per home, from the same facts the assistant sees, so table and chat agree."""
    out = []
    for p in toolbox.props.values():
        f = property_facts(p, toolbox.policy)
        route = f.get("cheapest_route") or {}
        cost = route.get("total_cost") if route.get("reaches_c") else None
        within = route.get("likely_within_cap") if cost else None
        out.append(
            {
                "Home": f["label"],
                "Rating": f"{f['current_band']} ({f['current_rating']})",
                "Rating gain to C": str(f["gap_to_c"] or "—"),  # one type for the table
                "Status": f["status"],
                "Cheapest route to C": cost or "—",
                "Likely within cap": {True: "Yes", False: "No", None: "—"}[within],
                "Fix early": "Yes" if f.get("fix_early_candidate") else "—",
                "Next step": f["next_action"],
            }
        )
    return out
