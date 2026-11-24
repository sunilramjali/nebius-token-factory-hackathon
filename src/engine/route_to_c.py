"""Deterministic route-to-C engine. Pure functions; no I/O.

Inputs come from the certificate's `suggested_improvements`. Each step carries the cumulative
SAP score *after* that step in register order (`rating_after`), so the per-step uplift is
`rating_after - previous rating`.

Assumptions (see docs/decisions.md ADR-009):
- Category "3" steps are "further measures" outside the potential rating. They are never part
  of the main route; they are reported separately.
- Uplifts are measured in register order and treated as additive. A route made of the first k
  register steps (or of all main steps) has an exact final score; any other subset is an
  estimate, and an estimate that lands within BORDERLINE_MARGIN of C is flagged `borderline`.
- The route is the exact cheapest set of costed steps (mid-point cost) that reaches C, found by
  checking every subset (ADR-011). A route whose cap-counting spend fits the cap is preferred.
- Routes without fossil-fuel heating are preferred (ADR-012). If a route with a gas/oil boiler is
  cheaper in total, or is the only way to stay within the cap, it is returned as
  `alternative`. A boiler route is the main route only when nothing else reaches C.
- Flags consider both routes: if only the boiler alternative fits the cap, the home is still a
  lock-in candidate (`lock_in_needs_boiler`) and not an over-cap exemption.
- When the main route is borderline, `safer` is the cheapest route (same fossil rule) reaching
  C + BORDERLINE_MARGIN.
- Spend on fossil-fuel heating (policy `fossil_heating_improvement_types`) doesn't count toward
  the cap. The route shows total cost; cap checks use cap-counting cost only.
- A step with no cost is used only if C can't be reached without it. Then the cost is unknown:
  no cap verdict, no lock-in, no exemption from cost, and low confidence.
- `within_cap` uses the mid-point cap cost; `within_cap_worst_case` the high end;
  `cap_verdict_uncertain` when the low and high ends fall either side of the cap.
- The cost cap ignores the low-value (10% of property value) rule because we don't know the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from engine.policy import Policy

FURTHER_MEASURES_CATEGORY = "3"
BORDERLINE_MARGIN = 2  # an estimated final score below 69 + 2 is flagged borderline
MAX_EXACT_STEPS = 16  # subsets checked exactly up to 2**16; above that, greedy (never seen: max 13)

BANDS = [(92, "A"), (81, "B"), (69, "C"), (55, "D"), (39, "E"), (21, "F"), (1, "G")]

_MONEY = re.compile(r"£?\s*([\d,]+(?:\.\d+)?)")


def band_for_score(score: int) -> str:
    for floor, band in BANDS:
        if score >= floor:
            return band
    return "G"


def parse_cost(text: str | int | float | None) -> tuple[float, float] | None:
    """'£500 - £1,500' -> (500, 1500); '£1,000' -> (1000, 1000); None/'' -> None."""
    if text is None:
        return None
    if isinstance(text, int | float):
        return (float(text), float(text))
    amounts = [float(m.replace(",", "")) for m in _MONEY.findall(text)]
    if not amounts:
        return None
    lo, hi = min(amounts[:2]), max(amounts[:2])
    return (lo, hi)


@dataclass(frozen=True)
class Step:
    sequence: int
    rating_after: int | None
    cost_text: str | int | float | None = None
    category: str | None = None
    improvement_type: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Certificate:
    certificate_number: str
    current_score: int
    lodgement_date: date
    potential_score: int | None = None
    steps: tuple[Step, ...] = ()
    uprn: int | None = None


@dataclass(frozen=True)
class RouteStep:
    sequence: int
    improvement_type: str | None
    description: str | None
    uplift: int
    cost_low: float | None
    cost_high: float | None
    cost_mid: float | None
    cumulative_score: int
    cumulative_band: str
    cumulative_cost_low: float
    cumulative_cost_high: float
    cumulative_cost_mid: float
    counts_toward_cap: bool = True


@dataclass(frozen=True)
class RouteSummary:
    """A secondary route (cheaper boiler alternative, or safer margin route)."""

    steps: tuple[RouteStep, ...]
    final_score: int
    cost_low: float
    cost_high: float
    cost_mid: float
    cap_cost_low: float
    cap_cost_high: float
    cap_cost_mid: float
    within_cap: bool


@dataclass(frozen=True)
class RouteResult:
    certificate_number: str
    current_score: int
    current_band: str
    below_c: bool
    gap_to_c: int
    steps: tuple[RouteStep, ...]
    further_measures: tuple[RouteStep, ...]
    final_score: int
    reaches_c: bool
    cost_low: float
    cost_high: float
    cost_mid: float
    within_cap: bool
    within_cap_worst_case: bool
    cert_age_years: float
    is_stale: bool
    confidence: str
    likely_exempt_cost_cap: bool
    lock_in_candidate: bool
    needs_survey: bool
    notes: tuple[str, ...] = field(default_factory=tuple)
    cap_cost_low: float = 0.0
    cap_cost_high: float = 0.0
    cap_cost_mid: float = 0.0
    cost_known: bool = True
    score_exact: bool = True
    borderline: bool = False
    cap_verdict_uncertain: bool = False
    exempt_reason: str | None = None  # "over_cap" | "cannot_reach_c" | None
    alternative: RouteSummary | None = None  # cheaper route that uses fossil heating
    safer: RouteSummary | None = None  # cheapest route to C + BORDERLINE_MARGIN, if borderline
    lock_in_needs_boiler: bool = False  # lock-in only via the fossil-heating alternative


def _uplifts(current: int, steps: list[Step]) -> list[tuple[Step, int]]:
    """Per-step uplift from cumulative ratings, in register sequence order."""
    out, prev = [], current
    for s in sorted(steps, key=lambda s: s.sequence):
        if s.rating_after is None:
            out.append((s, 0))
            continue
        out.append((s, max(s.rating_after - prev, 0)))
        prev = max(prev, s.rating_after)
    return out


def _mid(step: Step) -> float | None:
    cost = parse_cost(step.cost_text)
    return None if cost is None else (cost[0] + cost[1]) / 2


def _cost_per_point(item: tuple[Step, int]) -> tuple[int, float, int]:
    """Display order: cheapest per point first, unknown cost last."""
    step, uplift = item
    mid = _mid(step)
    if mid is None:
        return (1, float("inf"), step.sequence)
    if uplift <= 0:
        return (0, float("inf"), step.sequence)
    return (0, mid / uplift, step.sequence)


def _counts(step: Step, policy: Policy) -> bool:
    return step.improvement_type not in policy.fossil_heating_types


class _SubsetTable:
    """Sums over every subset of costed steps, built once per certificate (bitmask DP).

    best() then answers "cheapest subset reaching `gap`" for any gap / fossil rule in one pass.
    """

    def __init__(self, items: list[tuple[Step, int]], policy: Policy):
        self.items, self.policy, n = items, policy, len(items)
        self.n = n
        self.fossil_mask = sum(1 << i for i, (st, _) in enumerate(items) if not _counts(st, policy))
        size = 1 << n
        up, tot, cap, hi, cnt = [0] * size, [0.0] * size, [0.0] * size, [0.0] * size, [0] * size
        for i, (st, u) in enumerate(items):
            lo, h = parse_cost(st.cost_text)
            m = (lo + h) / 2
            c = 0.0 if self.fossil_mask >> i & 1 else m
            bit = 1 << i
            for mask in range(bit, bit << 1):  # masks whose highest bit is i
                prev = mask ^ bit
                up[mask] = up[prev] + u
                tot[mask] = tot[prev] + m
                cap[mask] = cap[prev] + c
                hi[mask] = hi[prev] + h
                cnt[mask] = cnt[prev] + 1
        self.up, self.tot, self.cap, self.hi, self.cnt = up, tot, cap, hi, cnt

    def best(self, gap: int, allow_fossil: bool) -> list[tuple[Step, int]] | None:
        """Preference: (1) cap-counting mid cost within the cap, (2) lowest total mid cost,
        (3) lowest cap-counting cost, (4) fewest steps, (5) lowest high-end cost,
        (6) earliest register sequences."""
        limit = self.policy.cost_cap_gbp
        fm = 0 if allow_fossil else self.fossil_mask
        best_key, best_mask = None, None
        for mask in range(1, 1 << self.n):
            if self.up[mask] < gap or mask & fm:
                continue
            key = (
                self.cap[mask] > limit,
                round(self.tot[mask], 2),
                round(self.cap[mask], 2),
                self.cnt[mask],
                self.hi[mask],
                self._seqs(mask),
            )
            if best_key is None or key < best_key:
                best_key, best_mask = key, mask
        return None if best_mask is None else self._pick(best_mask)

    def _seqs(self, mask: int) -> tuple[int, ...]:
        return tuple(sorted(self.items[i][0].sequence for i in range(self.n) if mask >> i & 1))

    def _pick(self, mask: int) -> list[tuple[Step, int]]:
        return [self.items[i] for i in range(self.n) if mask >> i & 1]


def _greedy(items: list[tuple[Step, int]], gap: int, policy: Policy, allow_fossil: bool):
    """Fallback above MAX_EXACT_STEPS (never seen in Newham data): greedy by cost per point."""
    pool = [it for it in items if allow_fossil or _counts(it[0], policy)]
    if sum(u for _, u in pool) < gap:
        return None
    chosen, score = [], 0
    for it in sorted(pool, key=_cost_per_point):
        if score >= gap:
            break
        chosen.append(it)
        score += it[1]
    return chosen


class _Search:
    def __init__(self, costed: list[tuple[Step, int]], policy: Policy):
        self.costed, self.policy = costed, policy
        self.table = _SubsetTable(costed, policy) if len(costed) <= MAX_EXACT_STEPS else None

    def best(self, gap: int, allow_fossil: bool):
        if self.table is not None:
            return self.table.best(gap, allow_fossil)
        return _greedy(self.costed, gap, self.policy, allow_fossil)


def _route_mid(items: list[tuple[Step, int]]) -> float:
    return sum(_mid(s) or 0.0 for s, _ in items)


def _cap_mid(items: list[tuple[Step, int]], policy: Policy) -> float:
    return sum(_mid(s) or 0.0 for s, _ in items if _counts(s, policy))


def _select_route(
    main_uplifts: list[tuple[Step, int]], gap: int, policy: Policy
) -> tuple[list[tuple[Step, int]], bool, list | None, _Search | None, bool]:
    """Return (route, cost_known, cheaper fossil alternative, search, route allows fossil)."""
    useful = [it for it in main_uplifts if it[1] > 0]
    costed = [it for it in useful if _mid(it[0]) is not None]
    uncosted = [it for it in useful if _mid(it[0]) is None]
    search = _Search(costed, policy)
    clean = search.best(gap, allow_fossil=False)
    any_ = search.best(gap, allow_fossil=True)
    if clean is not None:
        alt = None
        if any_ is not None and any_ != clean:
            cheaper = _route_mid(any_) < _route_mid(clean) - 0.005
            limit = policy.cost_cap_gbp
            only_within_cap = _cap_mid(any_, policy) <= limit < _cap_mid(clean, policy)
            if cheaper or only_within_cap:
                alt = any_
        return clean, True, alt, search, False
    if any_ is not None:
        return any_, True, None, search, True
    # C isn't reachable on costed steps alone: add uncosted steps, biggest uplift first.
    chosen, score = list(costed), sum(u for _, u in costed)
    for it in sorted(uncosted, key=lambda it: (-it[1], it[0].sequence)):
        if score >= gap:
            break
        chosen.append(it)
        score += it[1]
    return chosen, not any(_mid(s) is None for s, _ in chosen), None, None, True


def _summary(start: int, items: list[tuple[Step, int]], policy: Policy) -> RouteSummary:
    steps = _accumulate(start, sorted(items, key=_cost_per_point), policy)
    counted = [x for x in steps if x.counts_toward_cap]
    cap_mid = sum(x.cost_mid or 0.0 for x in counted)
    last = steps[-1]
    return RouteSummary(
        steps=tuple(steps),
        final_score=last.cumulative_score,
        cost_low=last.cumulative_cost_low,
        cost_high=last.cumulative_cost_high,
        cost_mid=last.cumulative_cost_mid,
        cap_cost_low=sum(x.cost_low or 0.0 for x in counted),
        cap_cost_high=sum(x.cost_high or 0.0 for x in counted),
        cap_cost_mid=cap_mid,
        within_cap=cap_mid <= policy.cost_cap_gbp,
    )


def _accumulate(
    start: int, items: list[tuple[Step, int]], policy: Policy | None = None
) -> list[RouteStep]:
    score, lo_t, hi_t, mid_t = start, 0.0, 0.0, 0.0
    out: list[RouteStep] = []
    for step, uplift in items:
        cost = parse_cost(step.cost_text)
        lo, hi = cost if cost else (None, None)
        mid = (lo + hi) / 2 if cost else None
        score += uplift
        lo_t += lo or 0.0
        hi_t += hi or 0.0
        mid_t += mid or 0.0
        out.append(
            RouteStep(
                sequence=step.sequence,
                improvement_type=step.improvement_type,
                description=step.description,
                uplift=uplift,
                cost_low=lo,
                cost_high=hi,
                cost_mid=mid,
                cumulative_score=score,
                cumulative_band=band_for_score(score),
                cumulative_cost_low=lo_t,
                cumulative_cost_high=hi_t,
                cumulative_cost_mid=mid_t,
                counts_toward_cap=_counts(step, policy) if policy else True,
            )
        )
    return out


def _is_exact(chosen: list[tuple[Step, int]], main_uplifts: list[tuple[Step, int]]) -> bool:
    """Final score is the register's own if the route is a register-order prefix of the
    useful steps, or all of them."""
    useful = [s.sequence for s, u in main_uplifts if u > 0]
    seqs = sorted(s.sequence for s, _ in chosen)
    return seqs == useful[: len(seqs)]


def route_to_c(cert: Certificate, policy: Policy, as_of: date) -> RouteResult:
    threshold = policy.band_c_threshold
    current = cert.current_score
    below_c = current < threshold
    notes: list[str] = []

    main = [s for s in cert.steps if s.category != FURTHER_MEASURES_CATEGORY]
    further = [s for s in cert.steps if s.category == FURTHER_MEASURES_CATEGORY]
    main_uplifts = _uplifts(current, main)

    gap = threshold - current
    if below_c:
        chosen, cost_known, alt_items, search, fossil_ok = _select_route(main_uplifts, gap, policy)
    else:
        chosen, cost_known, alt_items, search, fossil_ok = [], True, None, None, True
    route = _accumulate(current, sorted(chosen, key=_cost_per_point), policy)
    final = route[-1].cumulative_score if route else current
    score_exact = _is_exact(chosen, main_uplifts)

    further_start = max([s.rating_after or 0 for s in main] + [current])
    further_steps = tuple(_accumulate(further_start, _uplifts(further_start, further)))

    cost_low = route[-1].cumulative_cost_low if route else 0.0
    cost_high = route[-1].cumulative_cost_high if route else 0.0
    cost_mid = route[-1].cumulative_cost_mid if route else 0.0
    cap_steps = [s for s in route if s.counts_toward_cap]
    cap_low = sum(s.cost_low or 0.0 for s in cap_steps)
    cap_high = sum(s.cost_high or 0.0 for s in cap_steps)
    cap_mid = sum(s.cost_mid or 0.0 for s in cap_steps)
    cap = policy.cost_cap_gbp

    reaches_c = final >= threshold
    priced = reaches_c and cost_known
    within_cap = priced and cap_mid <= cap
    within_cap_worst = priced and cap_high <= cap
    cap_uncertain = priced and cap_low <= cap < cap_high
    borderline = below_c and reaches_c and not score_exact and final < threshold + BORDERLINE_MARGIN

    alternative = _summary(current, alt_items, policy) if alt_items else None
    alt_within_cap = alternative is not None and alternative.within_cap
    fits_cap = within_cap or alt_within_cap  # some known-cost route reaches C within the cap

    if below_c and main and not reaches_c:
        exempt_reason = "cannot_reach_c"
    elif below_c and main and priced and not fits_cap:
        exempt_reason = "over_cap"
    else:
        exempt_reason = None
    lock_in = below_c and fits_cap and as_of <= policy.lock_in_deadline

    age_years = (as_of - cert.lodgement_date).days / 365.25
    is_stale = age_years > policy.epc_validity_years
    missing_cost = not cost_known
    if below_c and not main:
        notes.append("Below C but the certificate lists no recommendations.")
    if missing_cost:
        notes.append("One or more route steps have no indicative cost.")
    if below_c and not reaches_c and main:
        notes.append("All recommended measures together do not reach C on this certificate.")
    if borderline:
        notes.append(f"Estimated final score {final} is within {BORDERLINE_MARGIN} points of C.")
    if any(not s.counts_toward_cap for s in route):
        notes.append("Route includes fossil-fuel heating, which doesn't count toward the cap.")
    if alternative:
        notes.append("A route with a gas or oil boiler is cheaper or fits the cap.")
    safer = None
    if borderline and search is not None:
        safer_items = search.best(gap + BORDERLINE_MARGIN, allow_fossil=fossil_ok)
        if safer_items:
            safer = _summary(current, safer_items, policy)
    if cap_uncertain:
        notes.append("Cost range spans the cap.")
    if is_stale:
        notes.append(f"Certificate is {age_years:.1f} years old (expired).")

    if is_stale or missing_cost or (below_c and not main):
        confidence = "low"
    elif age_years > 5:
        confidence = "medium"
    else:
        confidence = "high"

    return RouteResult(
        certificate_number=cert.certificate_number,
        current_score=current,
        current_band=band_for_score(current),
        below_c=below_c,
        gap_to_c=max(threshold - current, 0),
        steps=tuple(route),
        further_measures=further_steps,
        final_score=final,
        reaches_c=reaches_c,
        cost_low=cost_low,
        cost_high=cost_high,
        cost_mid=cost_mid,
        within_cap=within_cap if below_c else True,
        within_cap_worst_case=within_cap_worst if below_c else True,
        cert_age_years=round(age_years, 2),
        is_stale=is_stale,
        confidence=confidence,
        likely_exempt_cost_cap=exempt_reason is not None,
        lock_in_candidate=lock_in,
        needs_survey=confidence == "low",
        notes=tuple(notes),
        cap_cost_low=cap_low,
        cap_cost_high=cap_high,
        cap_cost_mid=cap_mid,
        cost_known=cost_known,
        score_exact=score_exact,
        borderline=borderline,
        cap_verdict_uncertain=cap_uncertain,
        exempt_reason=exempt_reason,
        alternative=alternative,
        safer=safer,
        lock_in_needs_boiler=lock_in and not within_cap,
    )
