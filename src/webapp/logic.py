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

# Landlord-facing text (docs/report_style.md). {cap}, {deadline} and {compliance} come from the
# policy. Short and plain: the reader is a busy landlord, often on a phone.
TEXT = {
    "title": "Get your rentals to EPC C.",
    "subtitle": "See which homes need work, what it might cost, and what to do first.",
    "demo_banner": "Example: 6 real rented homes in Newham. Addresses hidden.",
    # Pitch cards (hosted demo)
    "user_h": "For landlords",
    "user": "With a few rented homes and no energy adviser.",
    "problem_h": "The problem",
    "problem": "Rented homes would need EPC C by {compliance}. Where do you start?",
    "solution_h": "Our answer",
    "solution": "We find the cheapest fix for each home. An AI on Nebius explains it simply.",
    # Entry (full app)
    "add_homes": "Add your homes",
    "entry_help": "One home per row. Include the postcode.",
    "check": "Check my homes",
    "demo": "Try an example",
    "no_homes": "Add at least one home with a postcode.",
    "needs_input": "A few homes need a closer look",
    "pick": "Which one is it?",
    # Results
    "results": "Your homes",
    "tile_below": "Below C",
    "tile_fix_early": "Fix early",
    "tile_over_cap": "Likely over the cap",
    "tile_spend": "Estimated spend",
    "tile_spend_note": "Cheapest fixes, up to {cap} a home.",
    "fix_early_note": "Fix early: a C by {deadline} should count until the certificate expires.",
    "see_work": "See the work and who to call",
    "who_to_contact": "Who to contact",
    "draft_for": "Write a message to",
    "draft_note": "Copy it, add your address, and send it yourself.",
    "as_table": "Show as a table",
    "report": "Download the full report",
    # Chat
    "chat": "Ask a question",
    "chat_placeholder": "Ask anything about your homes",
    "suggest_1": "Which home should I fix first?",
    "suggest_2": "What will it cost in total?",
    "suggest_3": "Do I have to do this by law?",
    "working": "Where this answer came from",
    "chat_off": "The assistant is off. Add NEBIUS_API_KEY and NEBIUS_MODEL to turn it on.",
    "chat_error": "The assistant isn't available right now. Your results above still work.",
    "chat_limit": "That's the {n} free questions. Run the app yourself to ask more.",
    "chat_daily": "The demo has had a busy day. Please try again tomorrow.",
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
