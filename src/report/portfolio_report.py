"""One-page portfolio report (HTML, print-to-PDF friendly) from engine output.

Phase 0.5: used to build sample reports to show landlords. Numbers come only from
analytics.route_results / route_steps (the engine) — nothing is invented here.

Privacy / licence (ADR-007): sample reports show the postcode district only, never the full
address. Reports for a landlord's own properties may later show their own addresses.

A landlord's own report (from their CSV; see lookup/portfolio.py) shows their own labels and
the matched register address so they can check each match.

Usage:
  python -m report.portfolio_report --pick-samples            # 3 illustrative Newham properties
  python -m report.portfolio_report --property-id 100023... --property-id ...
  python -m report.portfolio_report --csv data/portfolios/mine.csv --out reports/mine.html
"""

from __future__ import annotations

import argparse
import html
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
from dotenv import load_dotenv

from engine.policy import Policy, load_policy
from lookup.portfolio import Resolved, resolve_csv

DISCLAIMER = (
    "Estimates from each home's current EPC certificate. A survey by an accredited energy "
    "assessor is required before any works. This is not legal or compliance advice."
)
LICENCE = (
    "Contains public sector information from the EPC register licensed under the Open "
    "Government Licence v3.0. Address/postcode data: © Crown copyright and database right; "
    "© Royal Mail Group Ltd — used for energy-efficiency purposes only."
)


@dataclass
class Step:
    order: int
    summary: str
    uplift: int
    cost_low: float | None
    cost_high: float | None
    cumulative_score: int
    cumulative_band: str
    counts_toward_cap: bool = True
    improvement_type: str | None = None


@dataclass
class Property:
    property_id: str
    label: str
    property_type: str | None
    registration_date: date
    current_score: int
    current_band: str
    final_score: int
    reaches_c: bool
    cost_low: float
    cost_high: float
    cost_mid: float
    within_cap: bool
    confidence: str
    lock_in_candidate: bool
    likely_exempt_cost_cap: bool
    needs_survey: bool
    notes: str
    steps: list[Step] = field(default_factory=list)
    register_address: str | None = None  # only for a landlord's own report
    cap_cost_low: float | None = None  # spend counting toward the cap (None: same as cost)
    cap_cost_high: float | None = None
    cost_known: bool = True
    borderline: bool = False
    score_exact: bool = True
    cap_verdict_uncertain: bool = False
    exempt_reason: str | None = None  # "over_cap" | "cannot_reach_c" | None
    lock_in_needs_boiler: bool = False
    alternative: list[Step] = field(default_factory=list)  # cheaper route with a gas/oil boiler
    safer: list[Step] = field(default_factory=list)  # route aiming 2 points above C

    @property
    def cap_low(self) -> float:
        return self.cost_low if self.cap_cost_low is None else self.cap_cost_low

    @property
    def cap_high(self) -> float:
        return self.cost_high if self.cap_cost_high is None else self.cap_cost_high


def gbp(x: float | None) -> str:
    return "—" if x is None else f"£{x:,.0f}"


def cost_range(lo: float | None, hi: float | None) -> str:
    if lo is None:
        return "cost not given"
    # "£400 to £950", not an en dash: easier for landlords to read (docs/report_style.md).
    return gbp(lo) if lo == hi else f"{gbp(lo)} to {gbp(hi)}"


SAMPLE_SQL = """
WITH base AS (
    SELECT r.*, c.tenure, c.registration_date
    FROM analytics.route_results r
    JOIN marts.fct_property_compliance c USING (property_id)
    WHERE c.is_private_rented AND r.below_c AND r.confidence = 'high'
      AND NOT r.cap_verdict_uncertain
)
-- 1. a cheap, clear lock-in
(SELECT property_id FROM base
 WHERE lock_in_candidate AND NOT borderline AND cost_mid < 3000 AND gap_to_c >= 5
 ORDER BY certificate_number LIMIT 1)
UNION ALL
-- 2. a borderline lock-in with a safer route: about half of reachable homes look like this
(SELECT property_id FROM base
 WHERE lock_in_candidate AND NOT lock_in_needs_boiler AND borderline
   AND safer_cost_mid IS NOT NULL AND safer_within_cap AND cost_mid BETWEEN 3000 AND 9000
 ORDER BY certificate_number LIMIT 1)
UNION ALL
-- 3. likely over the cap
(SELECT property_id FROM base
 WHERE exempt_reason = 'over_cap' AND NOT borderline
 ORDER BY certificate_number LIMIT 1)
"""


SAMPLE_NOTE = (
    "Three real Newham rented homes (anonymised), chosen to show different situations: a "
    "cheap fix, a borderline case, and one likely over the cap. They are not typical of every "
    "home."
)


def pick_samples(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(SAMPLE_SQL).fetchall()]


def load_properties(
    con: duckdb.DuckDBPyConnection,
    property_ids: list[str],
    labels: dict[str, str] | None = None,
    register_addresses: dict[str, str] | None = None,
    strict: bool = True,
) -> tuple[list[Property], list[str]]:
    """Load engine output. Returns (properties, ids with no engine result).

    `labels` replaces the anonymised "Property N — type, district" label (own-portfolio
    reports). With strict=True a missing id is an error.
    """
    props, missing = [], []
    for i, pid in enumerate(property_ids, start=1):
        row = con.execute(
            """
            SELECT r.property_id, split_part(c.postcode, ' ', 1) AS district, c.property_type,
                   c.registration_date, r.current_score, r.current_band, r.final_score,
                   r.reaches_c, r.cost_low, r.cost_high, r.cost_mid, r.within_cap,
                   r.confidence, r.lock_in_candidate, r.likely_exempt_cost_cap,
                   r.needs_survey, r.notes, r.cap_cost_low, r.cap_cost_high, r.cost_known,
                   r.borderline, r.score_exact, r.cap_verdict_uncertain, r.exempt_reason,
                   r.lock_in_needs_boiler
            FROM analytics.route_results r
            JOIN marts.fct_property_compliance c USING (property_id)
            WHERE r.property_id = ?
            """,
            [pid],
        ).fetchone()
        if row is None:
            if strict:
                raise SystemExit(f"property {pid} not found in analytics.route_results")
            missing.append(pid)
            continue
        (_, district, ptype, reg, cur, band, final, reaches, lo, hi, mid, within, conf, lock,
         exempt, survey, notes, cap_lo, cap_hi, known, border, exact, uncertain,
         reason, needs_boiler) = row  # fmt: skip
        label = (labels or {}).get(pid) or (
            f"Property {i} — {(ptype or 'home').capitalize()}, {district or 'Newham'}"
        )
        p = Property(pid, label, ptype, reg, cur, band, final, reaches, lo, hi, mid, within,
                     conf, lock, exempt, survey, notes or "")  # fmt: skip
        p.register_address = (register_addresses or {}).get(pid)
        p.cap_cost_low, p.cap_cost_high, p.cost_known = cap_lo, cap_hi, known
        p.borderline, p.score_exact = border, exact
        p.cap_verdict_uncertain, p.exempt_reason = uncertain, reason
        p.lock_in_needs_boiler = bool(needs_boiler)
        groups = {"route": p.steps, "alternative": p.alternative, "safer": p.safer}
        for kind, order, summary, typ, uplift, slo, shi, cs, cb, counts in con.execute(
            """
            SELECT s.step_type, s.step_order, coalesce(f.improvement_summary, s.improvement_type),
                   s.improvement_type, s.uplift, s.cost_low, s.cost_high,
                   s.cumulative_score, s.cumulative_band, s.counts_toward_cap
            FROM analytics.route_steps s
            LEFT JOIN marts.fct_recommendations_for_engine f
              ON f.certificate_number = s.certificate_number AND f.sequence = s.sequence
            WHERE s.property_id = ? AND s.step_type IN ('route', 'alternative', 'safer')
            ORDER BY s.step_type, s.step_order
            """,
            [pid],
        ).fetchall():
            groups[kind].append(
                Step(
                    order,
                    summary or typ or UNNAMED_MEASURE,
                    uplift,
                    slo,
                    shi,
                    cs,
                    cb,
                    counts is not False,
                    typ,
                )  # fmt: skip
            )
        props.append(p)
    return props, missing


UNNAMED_MEASURE = "Measure not described on the certificate"
NOT_COUNTED = " <span class='muted'>(doesn't count toward the cap)</span>"

# Works on shared parts of a building (walls, roof, floor, solar). In a flat these usually need
# the freeholder's consent. Display hint only; the engine doesn't use it.
SHARED_FABRIC_TYPES = {"A", "A2", "A3", "B", "B4", "Q", "W", "W1", "W2", "U", "N", "V", "Z1"}
FLAT_TYPES = {"flat", "maisonette"}

CONFIDENCE_TEXT = {
    "high": "recent certificate, full cost data",
    "medium": "certificate 5–10 years old",
    "low": "old certificate or missing costs",
}


@dataclass(frozen=True)
class Status:
    key: str  # stable id, used by the action list and tests
    colour: str  # green | amber | red | grey
    label: str


def status(p: Property, policy: Policy) -> Status:
    """One status per home. Wording rules: docs/report_style.md."""
    if p.current_score >= policy.band_c_threshold:
        return Status("done", "green", "Already C")
    if p.lock_in_needs_boiler:
        return Status("boiler", "amber", "Fix early (needs a boiler)")
    if p.lock_in_candidate and p.borderline:
        return Status("borderline", "amber", "Fix early (borderline)")
    if p.lock_in_candidate and p.cap_verdict_uncertain:
        return Status("quotes", "amber", "Fix early (check quotes)")
    if p.lock_in_candidate:
        return Status("fix", "green", "Fix early")
    if p.exempt_reason == "over_cap":
        return Status("over_cap", "red", "Likely over the cap")
    if p.exempt_reason == "cannot_reach_c":
        return Status("cannot", "red", "Can't reach C")
    return Status("survey", "grey", "Survey first")


def _caveats(p: Property, policy: Policy) -> list[str]:
    out = []
    if p.borderline:
        out.append(
            f"This route only just reaches C ({p.final_score}, an estimate). A survey may find "
            "it a point or two short."
        )
    if p.cap_verdict_uncertain:
        out.append(
            f"The cost range spans the £{policy.cost_cap_gbp:,} cap, so real quotes will decide "
            "whether the cap applies."
        )
    if any(not s.counts_toward_cap for s in p.steps):
        out.append(
            "This route includes a new gas or oil boiler. That cost doesn't count toward the cap."
        )
    if (p.property_type or "").lower() in FLAT_TYPES and any(
        s.improvement_type in SHARED_FABRIC_TYPES for s in p.steps
    ):
        out.append("In a flat, wall, roof, floor or solar works usually need the freeholder's OK.")
    return out


def _counted(lo: float, hi: float, clo: float, chi: float, policy: Policy) -> str:
    if (clo, chi) == (lo, hi):
        return ""
    return f" {cost_range(clo, chi)} of this counts toward the £{policy.cost_cap_gbp:,} cap."


def _totals(steps: list[Step]) -> tuple[float, float, float, float]:
    """(low, high, low counting toward the cap, high counting toward the cap)."""
    lo = sum(s.cost_low or 0 for s in steps)
    hi = sum(s.cost_high or 0 for s in steps)
    clo = sum(s.cost_low or 0 for s in steps if s.counts_toward_cap)
    chi = sum(s.cost_high or 0 for s in steps if s.counts_toward_cap)
    return lo, hi, clo, chi


def _within_cap(steps: list[Step], policy: Policy) -> bool:
    _, _, clo, chi = _totals(steps)
    return (clo + chi) / 2 <= policy.cost_cap_gbp


def secondary_routes(p: Property, policy: Policy) -> str:
    e = html.escape
    cap = f"£{policy.cost_cap_gbp:,}"
    out = []
    if p.safer:
        lo, hi, _, _ = _totals(p.safer)
        names = ", ".join(e(s.summary) for s in p.safer)
        cap_note = "" if _within_cap(p.safer, policy) else f" Likely over the {cap} cap."
        out.append(
            f"<p class='alt'><strong>Safer route: about {cost_range(lo, hi)}.</strong> "
            f"It aims for {p.safer[-1].cumulative_score}, leaving room for error.{cap_note} "
            f"Measures: {names}.</p>"
        )
    if p.alternative:
        lo, hi, clo, chi = _totals(p.alternative)
        names = ", ".join(e(s.summary) for s in p.alternative)
        cheaper = (lo + hi) / 2 < (p.cost_low + p.cost_high) / 2
        lead = "Cheaper option with a boiler" if cheaper else "Option with a boiler"
        only = (
            f" It's the only option likely within the {cap} cap."
            if _within_cap(p.alternative, policy) and not p.within_cap
            else ""
        )
        out.append(
            f"<p class='alt'><strong>{lead}: about {cost_range(lo, hi)}.</strong>"
            f"{_counted(lo, hi, clo, chi, policy)}{only} Measures: {names}. The 2030 heating "
            "standard isn't finalised, so check it before choosing a new gas or oil boiler.</p>"
        )
    return "".join(out)


def verdict(p: Property, policy: Policy) -> str:
    """Status, a one-line cost, then short reasons. Every sentence ≤ 25 words."""
    st = status(p, policy)
    cap = f"£{policy.cost_cap_gbp:,}"
    deadline = f"{policy.lock_in_deadline:%-d %B %Y}"
    cost = cost_range(p.cost_low, p.cost_high)
    counted = _counted(p.cost_low, p.cost_high, p.cap_low, p.cap_high, policy)
    lock_in = (
        f"A C certificate by {deadline} should count as compliant until it expires. "
        "Costs exclude the new EPC."
    )
    if st.key == "done":
        lines = ["No works needed now. Check again before the certificate expires."]
        head = st.label
    elif st.key == "boiler":
        head = f"{st.label}"
        lines = [
            f"Without a boiler, reaching C costs about {cost}, likely over the {cap} cap.",
            "The boiler option below is likely within the cap.",
            lock_in,
        ]
    elif st.key in {"fix", "borderline", "quotes"}:
        head = f"{st.label}: about {cost}"
        lines = [f"Likely within the {cap} cap, using the middle of the cost range.{counted}"]
        lines.append(lock_in)
    elif st.key == "over_cap":
        head = f"{st.label}: about {cost}"
        lines = [
            f"Reaching C is likely to cost more than the {cap} cap.{counted}",
            f"You may be able to register an exemption after spending up to {cap}.",
            "The exemption process for the 2030 rules isn't published yet.",
        ]
    elif st.key == "cannot":
        head = st.label
        lines = [
            "The certificate's measures don't reach C, even all together.",
            f"After spending up to {cap}, you may be able to register an exemption.",
            "A survey may find other measures.",
        ]
    else:
        head = st.label
        lines = (
            ["The certificate gives no cost for a measure needed to reach C."]
            if p.reaches_c and not p.cost_known
            else ["The certificate doesn't give enough detail to plan works."]
        )
        lines.append("Book a survey and get quotes before deciding.")
    body = " ".join(lines)
    extra = "".join(f"<br><span class='muted'>{c}</span>" for c in _caveats(p, policy))
    return f"<span class='status s{st.colour}'>{e_(head)}</span><br>{body}{extra}"


def details_html(cards: list[str]) -> str:
    """Property cards. The heading is wrapped with the first card so a page break can never
    leave it alone at the bottom of a page."""
    if not cards:
        return "<h2>Property by property</h2>"
    first = f"<div class='keep'><h2>Property by property</h2>{cards[0]}</div>"
    return first + "".join(cards[1:])


def rules_in_brief(policy: Policy) -> str:
    """Proposed rules, worded as proposals. Every value comes from config/policy.yaml."""
    d = "%-d %B %Y"
    return (
        "These are the government's proposals (response of Jan 2026). "
        "<strong>They are not yet law.</strong> "
        f"Rented homes would need EPC C by <strong>{policy.compliance_deadline:{d}}</strong>. "
        f"Landlords would not have to spend more than <strong>£{policy.cost_cap_gbp:,}</strong> "
        f"per home, or less for homes worth under £{policy.low_value_threshold_gbp:,}. "
        "Spend on gas or oil heating wouldn't count toward that limit. "
        f"A C reached by <strong>{policy.lock_in_deadline:{d}}</strong> on today's EPC method "
        f"should count as compliant until that certificate expires (up to "
        f"{policy.epc_validity_years} years). Check the final rules before acting."
    )


def e_(text: str) -> str:
    return html.escape(text)


ACTIONS = {
    # status key -> (priority, action template with {homes})
    "fix": (1, "Get quotes for {homes}. Then get a new EPC before {deadline}."),
    "quotes": (2, "Get quotes for {homes}: the cost range spans the {cap} cap."),
    "borderline": (3, "Ask an assessor whether the cheapest route really reaches C for {homes}."),
    "boiler": (
        4,
        "Decide on heating for {homes}: they're likely within the cap only with a boiler.",
    ),
    "over_cap": (5, "Ask an assessor to confirm costs for {homes}. An exemption may be possible."),
    "cannot": (6, "Book a survey for {homes} to look for other measures."),
    "survey": (7, "Book a survey for {homes}: costs are missing or the certificate is old."),
}


def _homes(labels: list[str]) -> str:
    if len(labels) > 3:
        labels = labels[:3] + [f"{len(labels) - 3} more"]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def next_actions(props: list[Property], policy: Policy) -> list[str]:
    """Up to 5 portfolio-level actions, most valuable first."""
    by_key: dict[str, list[str]] = {}
    for p in props:
        by_key.setdefault(status(p, policy).key, []).append(p.label)
    out = []
    for key, (_, template) in sorted(ACTIONS.items(), key=lambda kv: kv[1][0]):
        if key in by_key:
            out.append(
                template.format(
                    homes=_homes(by_key[key]),
                    deadline=f"{policy.lock_in_deadline:%-d %B %Y}",
                    cap=f"£{policy.cost_cap_gbp:,}",
                )
            )
    if not out:
        out.append("No works needed now. Check again before each certificate expires.")
    return out[:5]


def estimated_spend(p: Property, policy: Policy) -> tuple[float, float] | None:
    """Spend if the landlord follows the route, stopping at the cap. None if unknown."""
    if p.current_score >= policy.band_c_threshold or not p.reaches_c or not p.cost_known:
        return None
    cap = policy.cost_cap_gbp
    if p.lock_in_needs_boiler and p.alternative:  # the within-cap route is the boiler one
        lo, hi, clo, chi = _totals(p.alternative)
    else:
        lo, hi, clo, chi = p.cost_low, p.cost_high, p.cap_low, p.cap_high
    return (min(clo, cap) + lo - clo, min(chi, cap) + hi - chi)


NEEDS_INPUT_MESSAGES = {
    "ambiguous": "Several homes match. Add the right UPRN in a <code>uprn</code> column:",
    "no_match": "No home with that number found at this postcode on the EPC register. "
    "Check the house or flat number.",
    "no_candidates": "No EPCs found for this postcode. Check it; the home may have no EPC "
    "or be outside the area covered (Newham).",
    "no_postcode": "Add a postcode.",
    "duplicate": "Listed more than once; counted once.",
    "no_data": "Found on the EPC register, but its certificate details aren't available "
    "yet. It may need a new EPC.",
}


MAX_OPTIONS = 8


def reg_line(p: Property) -> str:
    if not p.register_address:
        return ""
    return f"Register record: {html.escape(p.register_address)} · "


def render_needs_input(items: list[Resolved]) -> str:
    if not items:
        return ""
    e = html.escape
    lis = []
    for r in items:
        msg = NEEDS_INPUT_MESSAGES.get(r.status, e(r.status))
        options = r.options
        if len(options) > MAX_OPTIONS:  # a big block: listing every flat doesn't help
            msg = (
                f"{len(options)} homes share this address. Add the flat number to the "
                "address, or the right UPRN in a <code>uprn</code> column."
            )
            options = []
        opts = "".join(f"<li><code>{u}</code> — {e(addr)}</li>" for u, addr in options)
        lis.append(
            f"<li><strong>{e(r.row.label)}</strong> (line {r.row.line}): {msg}"
            + (f"<ul>{opts}</ul>" if opts else "")
            + "</li>"
        )
    return (
        f"<section class='keep-head'><h2>Needs your input ({len(items)})</h2>"
        f"<p class='muted'>These rows aren't in the figures above. Fix them in your list and "
        f"re-run.</p><ul>{''.join(lis)}</ul></section>"
    )


def render(
    props: list[Property],
    policy: Policy,
    title: str,
    as_of: date,
    needs_input: list[Resolved] | None = None,
    note: str | None = None,
) -> str:
    below = [p for p in props if p.current_score < policy.band_c_threshold]
    lock = [p for p in props if p.lock_in_candidate]
    exempt = [p for p in props if p.likely_exempt_cost_cap]
    spends = [x for x in (estimated_spend(p, policy) for p in props) if x is not None]
    spend_lo, spend_hi = sum(x[0] for x in spends), sum(x[1] for x in spends)
    order = sorted(props, key=lambda p: (ACTIONS.get(status(p, policy).key, (9,))[0], p.cost_mid))
    actions = "".join(f"<li>{e_(a)}</li>" for a in next_actions(props, policy))

    e = html.escape

    def cost_cell(p: Property) -> str:
        return (
            "—"
            if status(p, policy).key in {"done", "survey"}
            else cost_range(p.cost_low, p.cost_high)
        )

    rows = "".join(
        f"<tr><td>{i}</td><td>{e(p.label)}</td><td><span class='band b{p.current_band}'>"
        f"{p.current_band}</span> {p.current_score}</td><td>{cost_cell(p)}</td>"
        f"<td><span class='status s{status(p, policy).colour}'>{status(p, policy).label}</span>"
        f"</td></tr>"
        for i, p in enumerate(order, start=1)
    )

    cards = []
    for p in order:
        end_band = p.steps[-1].cumulative_band if p.steps else p.current_band
        steps = "".join(
            f"<tr><td>{s.order}</td><td>{e(s.summary)}"
            f"{'' if s.counts_toward_cap else NOT_COUNTED}"
            f"</td><td>+{s.uplift}</td>"
            f"<td>{cost_range(s.cost_low, s.cost_high)}</td>"
            f"<td><span class='band b{s.cumulative_band}'>{s.cumulative_band}</span> "
            f"{s.cumulative_score}</td></tr>"
            for s in p.steps
        )
        steps_html = (
            f"<table><tr><th>#</th><th>Measure</th><th>Rating gain</th><th>Indicative cost</th>"
            f"<th>Rating after</th></tr>{steps}</table>"
            if steps
            else "<p class='muted'>No recommended measures on the certificate.</p>"
        )
        cards.append(
            f"""<section class="card">
  <h3>{e(p.label)}</h3>
  <p class="muted">{reg_line(p)}Certificate from {p.registration_date:%b %Y}
     · costs as of {p.registration_date.year} · {CONFIDENCE_TEXT.get(p.confidence, "")}</p>
  <p>Now: <span class="band b{p.current_band}">{p.current_band}</span> {p.current_score}
     &nbsp;→&nbsp; after the route below: <span class="band b{end_band}">{end_band}</span>
     {p.final_score}{"" if p.score_exact else " (estimate)"}</p>
  <p class="verdict">{verdict(p, policy)}</p>
  {steps_html}
  {secondary_routes(p, policy)}
</section>"""
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>
<style>
  @page {{ size: A4; margin: 14mm; }}
  body {{ font: 13px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1d1d1f; max-width: 780px; margin: 24px auto; padding: 0 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 22px 0 8px; }}
  h3 {{ font-size: 14px; margin: 0 0 4px; }}
  .muted {{ color: #6e6e73; margin: 2px 0 8px; }}
  .banner {{ background: #fff4e5; border: 1px solid #f5c26b; padding: 8px 12px;
            border-radius: 6px; margin: 12px 0; }}
  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }}
  .kpi {{ border: 1px solid #e5e5ea; border-radius: 8px; padding: 8px 10px; }}
  .kpi b {{ display: block; font-size: 18px; white-space: nowrap; }}
  table {{ width: 100%; border-collapse: collapse; margin: 6px 0; }}
  th, td {{ text-align: left; padding: 5px 6px; border-bottom: 1px solid #eee; }}
  th {{ font-weight: 600; color: #6e6e73; font-size: 12px; }}
  .card {{ border: 1px solid #e5e5ea; border-radius: 8px; padding: 12px 14px; margin: 10px 0;
          break-inside: avoid; }}
  .verdict {{ margin: 8px 0; }}
  .status {{ display: inline-block; font-weight: 700; padding: 1px 8px; border-radius: 10px; }}
  .sgreen {{ background: #e3f4e8; color: #0b6b2f; }}
  .samber {{ background: #fff1d6; color: #8a5300; }}
  .sred {{ background: #fde4e4; color: #a01818; }} .sgrey {{ background: #ececf0; color: #45454b; }}
  .next {{ border: 2px solid #1d1d1f; border-radius: 8px; padding: 8px 14px;
           margin: 12px 0; }}
  .next h2 {{ margin: 4px 0; }} .next ol {{ margin: 4px 0; padding-left: 20px; }}
  .alt {{ margin: 6px 0; padding: 6px 10px; background: #f5f5f7; border-radius: 6px; }}
  .band {{ display: inline-block; min-width: 18px; text-align: center; color: #fff;
          border-radius: 4px; font-weight: 700; padding: 0 4px; }}
  .bA {{ background:#008054 }} .bB {{ background:#19b459 }} .bC {{ background:#8dce46 }}
  .bD {{ background:#ffd500; color:#1d1d1f }} .bE {{ background:#fcaa65 }}
  .bF {{ background:#ef8023 }} .bG {{ background:#e9153b }}
  footer {{ color: #6e6e73; font-size: 11px; margin-top: 18px; }}
  /* Print layout: never strand a heading at the bottom of a page or split a card/row. */
  h1, h2, h3 {{ break-after: avoid; page-break-after: avoid; }}
  .keep, .card, .next, .kpis, .banner, footer, tr, .alt {{ break-inside: avoid;
         page-break-inside: avoid; }}
  .keep-head > h2 + * {{ break-before: avoid; }}
  thead {{ display: table-header-group; }}
  p {{ orphans: 3; widows: 3; }}
  @media print {{
    body {{ margin: 0 auto; }}
    .details {{ break-before: page; page-break-before: always; }}
  }}
</style></head><body>
<h1>{e(title)}</h1>
<p class="muted">Prepared {as_of:%-d %B %Y} from public EPC register data.</p>
{f'<p class="muted">{e(note)}</p>' if note else ""}
<div class="banner">{DISCLAIMER}</div>

<section class="next"><h2>What to do next</h2><ol>{actions}</ol></section>

<div class="kpis">
  <div class="kpi"><b>{len(below)} of {len(props)}</b>below EPC C</div>
  <div class="kpi"><b>{len(lock)}</b>worth fixing early</div>
  <div class="kpi"><b>{len(exempt)}</b>likely over the cap or can't reach C</div>
  <div class="kpi"><b>{cost_range(spend_lo, spend_hi)}</b>likely spend, up to the cap
    ({len(spends)} of {len(below)} homes with known costs)</div>
</div>

<section class="keep"><h2>Your homes</h2>
<table><thead><tr><th>#</th><th>Property</th><th>Rating now</th><th>Cost to reach C</th>
<th>Status</th></tr></thead>{rows}</table></section>

<section class="keep"><h2>The rules in brief</h2>
<p>{rules_in_brief(policy)}</p></section>

<section class="details">
{details_html(cards)}
</section>
{render_needs_input(needs_input or [])}

<footer>Costs are the EPC register's indicative ranges from when each certificate was made. They
may be out of date. Each route is the cheapest mix of the certificate's measures that reaches C.
Measures are listed cheapest per rating gain first. Ratings after each measure are estimates.
Costs exclude the new EPC needed to record a C.<br>{LICENCE}
</footer>
</body></html>
"""


def main(argv: list[str] | None = None) -> None:
    import os

    load_dotenv()
    p = argparse.ArgumentParser(prog="report.portfolio_report")
    p.add_argument("--target", default=os.environ.get("DUCKDB_TARGET", "md:route_to_c"))
    p.add_argument("--property-id", action="append", default=[])
    p.add_argument("--pick-samples", action="store_true")
    p.add_argument("--csv", help="landlord portfolio CSV (address, postcode, label, uprn)")
    p.add_argument("--title")
    p.add_argument("--out")
    p.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = p.parse_args(argv)

    con = duckdb.connect(args.target, read_only=False)
    if args.csv:
        props, needs_input = report_from_csv(con, args.csv)
        note = None
        title = args.title or "Your rental portfolio — route to EPC C"
        out = Path(args.out or "reports/portfolio.html")
    else:
        ids = args.property_id or (pick_samples(con) if args.pick_samples else [])
        if not ids:
            raise SystemExit("give --csv, --property-id ... or --pick-samples")
        props, _ = load_properties(con, ids)
        needs_input = []
        title = args.title or "Sample landlord portfolio — Newham"
        out = Path(args.out or "reports/sample_portfolio.html")
        note = SAMPLE_NOTE if args.pick_samples else None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(props, load_policy(), title, args.as_of, needs_input, note))
    print(f"wrote {out} ({len(props)} properties, {len(needs_input)} need input)")


def report_from_csv(
    con: duckdb.DuckDBPyConnection, path: str | Path
) -> tuple[list[Property], list[Resolved]]:
    """Resolve a landlord CSV to UPRNs and load their engine results."""
    resolved = resolve_csv(con, path)
    ok = [r for r in resolved if r.status in ("matched", "given")]
    needs_input = [r for r in resolved if r.status not in ("matched", "given")]
    ids = [str(r.uprn) for r in ok]
    labels = {str(r.uprn): r.row.label for r in ok}
    reg = {}
    if ids:
        reg = dict(
            con.execute(
                """
                select cast(uprn as varchar),
                    arg_max(concat_ws(', ', address_line1, address_line2, address_line3,
                                      address_line4, postcode), registration_date)
                from raw.epc_certificate_index
                where cast(uprn as varchar) in (select unnest(?))
                group by 1
                """,
                [ids],
            ).fetchall()
        )
    props, missing = load_properties(con, ids, labels, reg, strict=False)
    by_id = {str(r.uprn): r for r in ok}
    for pid in missing:
        by_id[pid].status = "no_data"
        needs_input.append(by_id[pid])
    needs_input.sort(key=lambda r: r.row.line)
    return props, needs_input


if __name__ == "__main__":
    main()
