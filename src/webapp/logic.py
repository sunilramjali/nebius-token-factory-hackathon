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
    "draft_note": "Add your details, then copy it or open it in your email.",
    # Suggested next steps after an answer, and the draft form
    "next_steps": "Next steps",
    "follow_who": "Who do I call for {home}?",
    "follow_work": "What work does {home} need?",
    "follow_write": "Write to {who}",
    "draft_ready": (
        "Here's a message for {who}. Add your details, then copy it or open it in your email."
    ),
    "form_name": "Your name",
    "form_address": "The home's address",
    "form_address_known": "Address from your list: {address}",
    "form_phone": "Phone (optional)",
    "form_fill": "Fill in the message",
    "open_email": "Open in your email app",
    "form_note": "Your details stay on this page. We don't send them to the AI.",
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
    SELECT r.*, c.property_type FROM analytics.route_results r
    JOIN marts.fct_property_compliance c USING (property_id)
    WHERE c.is_private_rented AND r.confidence = 'high' AND r.uprn IS NOT NULL
)
SELECT slot, uprn, property_type FROM (
  (SELECT 1 AS slot, uprn, property_type FROM base WHERE lock_in_candidate AND NOT borderline
     AND NOT cap_verdict_uncertain AND NOT lock_in_needs_boiler ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 2, uprn, property_type FROM base WHERE lock_in_candidate AND borderline
     AND NOT lock_in_needs_boiler AND safer_cost_mid IS NOT NULL ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 3, uprn, property_type FROM base WHERE lock_in_candidate AND cap_verdict_uncertain
     AND NOT borderline AND NOT lock_in_needs_boiler ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 4, uprn, property_type FROM base WHERE exempt_reason = 'over_cap' ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 5, uprn, property_type FROM base WHERE exempt_reason = 'cannot_reach_c'
     ORDER BY uprn LIMIT 1)
  UNION ALL
  (SELECT 6, uprn, property_type FROM base WHERE NOT below_c ORDER BY uprn LIMIT 1)
) ORDER BY slot
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


def nicknames(property_types: list[str | None]) -> list[str]:
    """Demo labels the way landlords talk: "Flat A", "Flat B", "House A". Built from the
    property type (open data) only. Never a made-up address: it could be someone's real home."""
    seen: dict[str, int] = {}
    out = []
    for t in property_types:
        kind = (t or "home").strip().capitalize()
        out.append(f"{kind} {chr(ord('A') + seen.get(kind, 0))}")
        seen[kind] = seen.get(kind, 0) + 1
    return out


def demo_homes(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    """(nickname, UPRN) for the 6 demo homes, in situation order."""
    rows = con.execute(DEMO_PORTFOLIO_SQL).fetchall()
    return list(zip(nicknames([r[2] for r in rows]), [r[1] for r in rows], strict=True))


def demo_entries(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    return [
        {"label": label, "address": "", "postcode": "", "uprn": str(uprn)}
        for label, uprn in demo_homes(con)
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


MAX_FOLLOW_UPS = 3


def follow_ups(calls: list[dict[str, Any]], toolbox: Toolbox) -> list[dict[str, Any]]:
    """Next steps to offer after an answer, picked by code from the tools the agent used.

    kind "draft" builds a quote request in code (no AI call); kind "ask" sends a question.
    """
    out: list[dict[str, Any]] = []
    helped: set[str] = set()
    for c in calls:
        res, args = c.get("result") or {}, c.get("args") or {}
        if c["name"] == "find_help" and res.get("needed"):
            p = toolbox.home(args.get("home"))
            helped.add(res["home"])
            for n in res["needed"][:2]:
                if p is not None:
                    out.append({"kind": "draft", "home": p.property_id, "trade": n["trade"],
                                "who": n["who"], "label_key": "follow_write"})  # fmt: skip
        elif c["name"] == "get_property" and "label" in res and res.get("gap_to_c"):
            out.append({"kind": "ask", "home": res["label"], "label_key": "follow_who"})
        elif c["name"] == "get_portfolio_summary":
            first = next((h for h in res.get("homes", []) if h["status"].startswith("Fix")), None)
            if first:
                out.append({"kind": "ask", "home": first["label"], "label_key": "follow_who"})
                out.append({"kind": "ask", "home": first["label"], "label_key": "follow_work"})
    seen, unique = set(), []
    for f in out:
        if f["kind"] == "ask" and f["label_key"] == "follow_who" and f["home"] in helped:
            continue  # already answered "who do I call" for this home
        key = (f["kind"], f.get("home"), f.get("trade"), f["label_key"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique[:MAX_FOLLOW_UPS]


def typed_addresses(resolved: list[Resolved]) -> dict[str, str]:
    """UPRN -> the address the landlord typed for that home, to fill their messages.

    Only the landlord's own text: never the register's address (licence-restricted,
    docs/data_access.md §3). Rows added by UPRN alone have no typed address.
    """
    out = {}
    for r in resolved:
        if r.status not in ("matched", "given") or r.uprn is None or not r.row.address:
            continue
        address = r.row.address
        pc = r.row.postcode
        if pc and pc.replace(" ", "").lower() not in address.replace(" ", "").lower():
            address = f"{address}, {pc}"
        out[str(r.uprn)] = address
    return out
