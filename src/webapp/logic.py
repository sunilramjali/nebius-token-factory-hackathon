"""Pure logic behind app.py: turn typed rows into UPRNs, and engine output into table rows.

Privacy (docs/data_access.md §3): register addresses are shown only for homes the landlord
typed (to settle an ambiguous match) and never reach the assistant. A home with no label gets
"Home N", not its address, because labels are sent to Nebius.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path
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
from report.portfolio_report import NEEDS_INPUT_MESSAGES, Property, Step

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
    # Hosted demo (DEMO_MODE): the pitch, then 6 example homes. No address entry.
    "demo_banner": (
        "Demo: 6 real rented homes in Newham, from the public EPC register. Addresses are "
        "hidden. Run it locally to check your own homes."
    ),
    "user": "**Who it's for:** a landlord with a few rented homes in Newham and no energy adviser.",
    "problem": (
        "**The problem:** under the government's proposals, rented homes would need EPC C by "
        "{compliance}. Spend would be capped at {cap} per home. Which homes need work, and "
        "what's the cheapest fix?"
    ),
    "solution": (
        "**The solution:** our engine works out each home's cheapest route to C from the EPC "
        "register. An AI agent on Nebius Token Factory explains it, using only the engine's "
        "figures."
    ),
    "chat_limit": "You've used the {n} demo questions. Run the app locally to ask more.",
    "chat_daily": "The demo has reached today's question limit. Please try again tomorrow.",
    "too_long": "Please keep questions under {n} characters.",
}

# Hosted demo limits, so a public link can't run up the Nebius bill (about $0.0007 a question).
DEMO_QUESTIONS_PER_VISITOR = 10
DEMO_QUESTIONS_PER_DAY = 300
MAX_QUESTION_CHARS = 500
DEMO_FILE = Path(__file__).resolve().parents[2] / "examples" / "demo_portfolio.json"

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


def text(key: str, policy: Policy, **extra: Any) -> str:
    return TEXT[key].format(
        cap=f"£{policy.cost_cap_gbp:,}",
        deadline=f"{policy.lock_in_deadline:%-d %B %Y}",
        compliance=f"{policy.compliance_deadline:%-d %B %Y}",
        **extra,
    )


# --- hosted demo -------------------------------------------------------------------------

# Never exported: the register address is licence-restricted (docs/data_access.md §3).
_NOT_EXPORTED = {"register_address"}
_STEP_LISTS = ("steps", "alternative", "safer")


def dump_demo(props: list[Property]) -> dict[str, Any]:
    homes = []
    for p in props:
        d = {k: v for k, v in asdict(p).items() if k not in _NOT_EXPORTED}
        d["registration_date"] = p.registration_date.isoformat()
        homes.append(d)
    return {
        "note": "Engine results for 6 example homes. UPRNs and EPC fields only, no addresses "
        "(Open Government Licence v3.0). Made by scripts/export_demo.py.",
        "homes": homes,
    }


def load_demo(path: Path = DEMO_FILE) -> list[Property]:
    names = {f.name for f in fields(Property)}
    props = []
    for d in json.loads(Path(path).read_text())["homes"]:
        d = {k: v for k, v in d.items() if k in names and k not in _NOT_EXPORTED}
        d["registration_date"] = date.fromisoformat(d["registration_date"])
        for key in _STEP_LISTS:
            d[key] = [Step(**s) for s in d.get(key, [])]
        props.append(Property(**d))
    return props


class DailyCounter:
    """Questions asked today, across all visitors of one server process."""

    def __init__(self, limit: int):
        self.limit = limit
        self._day, self._count = date.today(), 0
        self._lock = threading.Lock()

    def take(self, today: date | None = None) -> bool:
        """Count one question. False if today's limit is already reached."""
        today = today or date.today()
        with self._lock:
            if today != self._day:
                self._day, self._count = today, 0
            if self._count >= self.limit:
                return False
            self._count += 1
            return True


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
