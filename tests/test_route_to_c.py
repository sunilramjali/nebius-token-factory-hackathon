from datetime import date

import pytest

from engine.policy import Policy, load_policy
from engine.route_to_c import Certificate, Step, band_for_score, parse_cost, route_to_c

POLICY = Policy(
    band_c_threshold=69,
    cost_cap_gbp=10_000,
    lock_in_deadline=date(2029, 9, 30),
    epc_validity_years=10,
)
TODAY = date(2026, 11, 6)


def cert(current, steps=(), lodged=date(2024, 1, 1), potential=None, num="C1"):
    return Certificate(
        certificate_number=num,
        current_score=current,
        lodgement_date=lodged,
        potential_score=potential,
        steps=tuple(steps),
    )


# ---------- helpers ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("£500 - £1,500", (500, 1500)),
        ("£4,000 - £14,000", (4000, 14000)),
        ("£1,000", (1000, 1000)),
        (225, (225, 225)),
        (None, None),
        ("", None),
        ("not applicable", None),
    ],
)
def test_parse_cost(text, expected):
    assert parse_cost(text) == expected


@pytest.mark.parametrize(
    "score,band", [(92, "A"), (81, "B"), (80, "C"), (69, "C"), (68, "D"), (39, "E"), (1, "G")]
)
def test_band_for_score(score, band):
    assert band_for_score(score) == band


def test_policy_loads_from_config():
    p = load_policy()
    assert p.band_c_threshold == 69
    assert p.cost_cap_gbp == 10_000
    assert p.lock_in_deadline == date(2029, 9, 30)


# ---------- fixture cases from the brief ----------


def test_already_c():
    r = route_to_c(cert(72, [Step(1, 75, "£100 - £350", "5")]), POLICY, TODAY)
    assert not r.below_c and r.reaches_c and r.gap_to_c == 0
    assert r.steps == () and r.cost_mid == 0
    assert not r.lock_in_candidate and not r.likely_exempt_cost_cap


def test_d_cheap_fix_is_lock_in_candidate():
    r = route_to_c(cert(66, [Step(1, 70, "£100 - £350", "5", "E")]), POLICY, TODAY)
    assert r.below_c and r.gap_to_c == 3
    assert r.reaches_c and r.final_score == 70
    assert (r.cost_low, r.cost_high, r.cost_mid) == (100, 350, 225)
    assert r.within_cap and r.lock_in_candidate and not r.likely_exempt_cost_cap
    assert r.confidence == "high"


def test_real_newham_e_orders_by_cost_per_point():
    # Real Newham RdSAP 21.0.1 certificate: 41 -> 68 (A2) -> 78 (L2)
    steps = [
        Step(1, 68, "£900 - £1,200", "5", "A2"),
        Step(2, 78, "£800 - £1,600", "5", "L2"),
    ]
    r = route_to_c(cert(41, steps, potential=78), POLICY, TODAY)
    assert [s.improvement_type for s in r.steps] == ["A2", "L2"]
    assert [s.cumulative_score for s in r.steps] == [68, 78]
    assert r.cost_mid == 2250 and r.reaches_c and r.lock_in_candidate


def test_cheaper_per_point_step_goes_first_and_route_stops_at_c():
    steps = [
        Step(1, 62, "£4,000 - £6,000", "5", "Q"),  # +10 for £5,000 -> £500/pt
        Step(2, 66, "£100 - £300", "5", "E"),  # +4 for £200 -> £50/pt
        Step(3, 71, "£500 - £1,500", "5", "G"),  # +5 for £1,000 -> £200/pt
        Step(4, 75, "£2,000 - £4,000", "5", "W1"),  # not needed
    ]
    r = route_to_c(cert(52, steps), POLICY, TODAY)
    assert [s.improvement_type for s in r.steps] == ["E", "G", "Q"]
    assert r.final_score == 71 and r.reaches_c
    assert r.cost_mid == 200 + 1000 + 5000


def test_e_expensive_over_cap_is_likely_exempt():
    steps = [
        Step(1, 55, "£4,000 - £14,000", "5", "Q"),  # solid wall
        Step(2, 70, "£6,000 - £9,000", "5", "I"),
    ]
    r = route_to_c(cert(45, steps), POLICY, TODAY)
    assert r.reaches_c
    assert r.cost_mid == 9000 + 7500
    assert not r.within_cap and r.likely_exempt_cost_cap and not r.lock_in_candidate


def test_g_hits_cap_and_never_reaches_c():
    steps = [Step(1, 25, "£3,000 - £5,000", "5"), Step(2, 40, "£5,000 - £9,000", "5")]
    r = route_to_c(cert(10, steps), POLICY, TODAY)
    assert not r.reaches_c and r.final_score == 40
    assert r.likely_exempt_cost_cap and not r.lock_in_candidate
    assert any("do not reach C" in n for n in r.notes)


def test_missing_recommendations_needs_survey():
    r = route_to_c(cert(50, []), POLICY, TODAY)
    assert r.below_c and not r.reaches_c
    assert r.confidence == "low" and r.needs_survey
    assert not r.likely_exempt_cost_cap  # can't conclude exemption without data


# ---------- data-quality rules ----------


def test_further_measures_excluded_from_main_route():
    steps = [
        Step(1, 64, "£500 - £1,500", "5"),
        Step(2, 67, "£100 - £350", "5"),
        Step(3, 80, "£3,500 - £5,500", "3", "U"),  # solar PV: further measure
    ]
    r = route_to_c(cert(60, steps, potential=67), POLICY, TODAY)
    assert not r.reaches_c and r.final_score == 67
    assert [s.improvement_type for s in r.further_measures] == ["U"]
    assert r.further_measures[0].cumulative_score == 80


def test_missing_cost_goes_last_and_lowers_confidence():
    steps = [Step(1, 66, None, "5", "X"), Step(2, 70, "£200 - £400", "5", "E")]
    r = route_to_c(cert(60, steps), POLICY, TODAY)
    assert [s.improvement_type for s in r.steps] == ["E", "X"]
    assert r.confidence == "low" and r.needs_survey


def test_stale_certificate_is_low_confidence():
    r = route_to_c(
        cert(60, [Step(1, 70, "£100 - £350", "5")], lodged=date(2014, 5, 1)), POLICY, TODAY
    )
    assert r.is_stale and r.confidence == "low"


def test_medium_confidence_for_6_year_old_cert():
    r = route_to_c(
        cert(60, [Step(1, 70, "£100 - £350", "5")], lodged=date(2020, 6, 1)), POLICY, TODAY
    )
    assert r.confidence == "medium"


def test_no_lock_in_after_deadline():
    r = route_to_c(cert(66, [Step(1, 70, "£100 - £350", "5")]), POLICY, date(2029, 10, 1))
    assert r.within_cap and not r.lock_in_candidate


def test_worst_case_cap_flag():
    r = route_to_c(cert(60, [Step(1, 70, "£6,000 - £14,000", "5")]), POLICY, TODAY)
    assert r.within_cap and not r.within_cap_worst_case


# ---------- regression tests for the 2026-11-06 audit (ADR-011) ----------

FOSSIL = Policy(
    band_c_threshold=69,
    cost_cap_gbp=10_000,
    lock_in_deadline=date(2029, 9, 30),
    epc_validity_years=10,
    fossil_heating_types=frozenset({"I", "S", "T"}),
)


def test_policy_loads_fossil_heating_types_and_low_value_threshold():
    p = load_policy()
    assert {"I", "S", "T"} <= p.fossil_heating_types
    assert p.low_value_threshold_gbp == 100_000


def test_real_newham_flat_cheapest_subset_and_fossil_boiler_not_counted():
    # Real certificate 0000-2081-7281-2302-2885 (sample report "Property 3"), 57 -> 73.
    steps = [
        Step(1, 60, "£100 - £350", "5", "A"),
        Step(2, 66, "£4,000 - £14,000", "5", "Q"),
        Step(3, 67, "£20", "5", "E"),
        Step(4, 72, "£2,200 - £3,000", "5", "I"),
        Step(5, 73, "£3,300 - £6,500", "5", "O"),
    ]
    r = route_to_c(cert(57, steps, lodged=date(2022, 8, 11)), FOSSIL, TODAY)
    # Old greedy route was E, A, I, Q (£11,845, "likely exempt"). Loft insulation isn't needed.
    assert sorted(s.improvement_type for s in r.steps) == ["E", "I", "Q"]
    assert r.cost_mid == 20 + 2600 + 9000
    assert r.cap_cost_mid == 20 + 9000  # boiler excluded
    assert [s.counts_toward_cap for s in r.steps if s.improvement_type == "I"] == [False]
    assert r.within_cap and r.lock_in_candidate and not r.likely_exempt_cost_cap
    assert r.final_score == 69 and not r.score_exact and r.borderline
    assert r.cap_verdict_uncertain  # £4,020 to £14,020 counts: spans the cap


def test_cheapest_set_beats_greedy_cost_per_point():
    steps = [
        Step(1, 62, "£20", "5", "E"),  # +2, £10/pt: greedy takes it first
        Step(2, 70, "£480", "5", "G"),  # +8, £60/pt
        Step(3, 79, "£500", "5", "W"),  # +9, £55.6/pt
    ]
    r = route_to_c(cert(60, steps), POLICY, TODAY)
    assert [s.improvement_type for s in r.steps] == ["W"]  # greedy would pay £520
    assert r.cost_mid == 500


def test_boiler_route_offered_when_it_is_the_only_way_within_cap():
    steps = [
        Step(1, 69, "£10,500", "5", "Q"),  # +10, counts: total £10,500, over cap
        Step(2, 75, "£3,000", "5", "I"),  # +6, fossil: doesn't count
        Step(3, 79, "£8,000", "5", "W"),  # +4, counts
    ]
    r = route_to_c(cert(59, steps), FOSSIL, TODAY)
    assert [s.improvement_type for s in r.steps] == ["Q"]  # non-fossil route preferred
    assert not r.within_cap  # main route alone is over the cap...
    assert r.lock_in_candidate and r.lock_in_needs_boiler  # ...but the boiler route fits
    assert r.exempt_reason is None
    alt = r.alternative
    assert sorted(s.improvement_type for s in alt.steps) == ["I", "W"]
    assert alt.cost_mid == 11_000 and alt.cap_cost_mid == 8_000 and alt.within_cap


def test_non_fossil_route_preferred_and_cheaper_boiler_route_is_alternative():
    steps = [
        Step(1, 66, "£2,000", "5", "I"),  # +6 boiler, cheap
        Step(2, 70, "£3,000", "5", "W"),  # +4
        Step(3, 76, "£4,000", "5", "U"),  # +6
    ]
    r = route_to_c(cert(60, steps), FOSSIL, TODAY)
    assert sorted(s.improvement_type for s in r.steps) == ["U", "W"]  # £7,000, no boiler
    assert all(s.counts_toward_cap for s in r.steps)
    assert r.alternative is not None and r.alternative.cost_mid == 5_000
    assert any(not s.counts_toward_cap for s in r.alternative.steps)


def test_boiler_route_is_main_route_only_when_nothing_else_reaches_c():
    steps = [Step(1, 66, "£2,000", "5", "I"), Step(2, 70, "£3,000", "5", "W")]  # need both
    r = route_to_c(cert(60, steps), FOSSIL, TODAY)
    assert sorted(s.improvement_type for s in r.steps) == ["I", "W"]
    assert r.alternative is None


def test_safer_route_offered_for_borderline():
    steps = [
        Step(1, 70, "£9,000", "5", "Q"),  # +10
        Step(2, 72, "£100", "5", "E"),  # +2
        Step(3, 79, "£500", "5", "W"),  # +7
    ]
    r = route_to_c(cert(60, steps), POLICY, TODAY)
    assert r.borderline and r.final_score == 69
    assert r.safer is not None and r.safer.final_score >= 71
    assert r.safer.cost_mid >= r.cost_mid


def test_no_safer_route_when_not_borderline():
    r = route_to_c(cert(60, [Step(1, 66, "£100", "5"), Step(2, 69, "£200", "5")]), POLICY, TODAY)
    assert not r.borderline and r.safer is None


def test_missing_cost_is_never_a_lock_in_or_cost_exemption():
    r = route_to_c(cert(66, [Step(1, 70, None, "5", "X")]), POLICY, TODAY)
    assert r.reaches_c and not r.cost_known
    assert not r.within_cap and not r.lock_in_candidate and not r.likely_exempt_cost_cap
    assert r.confidence == "low" and r.needs_survey


def test_uncosted_step_skipped_when_costed_steps_reach_c():
    steps = [Step(1, 71, None, "5", "X"), Step(2, 75, "£200", "5", "E")]  # E: +4
    r = route_to_c(cert(66, steps), POLICY, TODAY)
    assert [s.improvement_type for s in r.steps] == ["E"]
    assert r.cost_known and r.lock_in_candidate and r.final_score == 70


def test_register_prefix_route_is_exact_not_borderline():
    steps = [Step(1, 66, "£100", "5", "A"), Step(2, 69, "£200", "5", "E")]
    r = route_to_c(cert(60, steps), POLICY, TODAY)
    assert r.final_score == 69 and r.score_exact and not r.borderline


def test_reordered_subset_landing_on_69_is_borderline():
    steps = [
        Step(1, 70, "£9,000", "5", "Q"),  # +10, not chosen
        Step(2, 72, "£100", "5", "E"),  # +2
        Step(3, 79, "£500", "5", "W"),  # +7
    ]
    r = route_to_c(cert(60, steps), POLICY, TODAY)
    assert sorted(s.improvement_type for s in r.steps) == ["E", "W"]
    assert r.final_score == 69 and not r.score_exact and r.borderline
    assert any("within 2 points" in n for n in r.notes)


def test_exempt_reasons_are_distinguished():
    over = route_to_c(cert(45, [Step(1, 70, "£12,000", "5", "Q")]), POLICY, TODAY)
    assert over.exempt_reason == "over_cap" and over.likely_exempt_cost_cap
    short = route_to_c(cert(30, [Step(1, 50, "£2,000", "5", "Q")]), POLICY, TODAY)
    assert short.exempt_reason == "cannot_reach_c" and short.likely_exempt_cost_cap
    ok = route_to_c(cert(66, [Step(1, 70, "£300", "5", "E")]), POLICY, TODAY)
    assert ok.exempt_reason is None


def test_zero_uplift_step_never_in_route():
    steps = [Step(1, 60, "£50", "5", "D"), Step(2, 70, "£300", "5", "E")]  # D adds nothing
    r = route_to_c(cert(60, steps), POLICY, TODAY)
    assert [s.improvement_type for s in r.steps] == ["E"]


# ---------- invariants on random certificates (independent brute force) ----------


def _brute_force_best(current, steps, policy, allow_fossil=True):
    """Independent reference: cheapest feasible costed subset, preferring cap-fitting ones."""
    import itertools

    items = []
    prev = current
    for s in sorted(steps, key=lambda s: s.sequence):
        up = max(s.rating_after - prev, 0)
        prev = max(prev, s.rating_after)
        c = parse_cost(s.cost_text)
        fossil = s.improvement_type in policy.fossil_heating_types
        if up > 0 and c is not None and (allow_fossil or not fossil):
            items.append((s, up, (c[0] + c[1]) / 2))
    best = None
    for k in range(1, len(items) + 1):
        for comb in itertools.combinations(items, k):
            if current + sum(x[1] for x in comb) < policy.band_c_threshold:
                continue
            total = sum(x[2] for x in comb)
            cap = sum(
                x[2] for x in comb if x[0].improvement_type not in policy.fossil_heating_types
            )
            key = (cap > policy.cost_cap_gbp, total)
            if best is None or key < best:
                best = key
    return best


def test_random_certificates_satisfy_invariants():
    import random

    rng = random.Random(20261106)
    types = ["A", "B", "E", "G", "I", "Q", "S", "T", "U", "W1"]
    for case in range(1500):
        current = rng.randint(5, 80)
        n = rng.randint(0, 8)
        score, steps = current, []
        for seq in range(1, n + 1):
            score += rng.choice([0, 1, 2, 3, 5, 8, 12])
            lo = rng.choice([20, 100, 350, 800, 2200, 4000, 9000])
            cost = None if rng.random() < 0.1 else f"£{lo:,} - £{lo * rng.choice([1, 2, 3]):,}"
            cat = "3" if rng.random() < 0.1 else "5"
            steps.append(Step(seq, min(score, 100), cost, cat, rng.choice(types)))
        c = cert(current, steps, lodged=date(rng.randint(2008, 2026), 1, 1))
        r = route_to_c(c, FOSSIL, TODAY)
        ctx = f"case {case}: {c}"

        # flags are mutually consistent
        assert not (r.lock_in_candidate and r.likely_exempt_cost_cap), ctx
        assert r.likely_exempt_cost_cap == (r.exempt_reason is not None), ctx
        if r.lock_in_candidate:
            assert r.below_c and r.reaches_c and r.cost_known, ctx
            if r.lock_in_needs_boiler:
                assert not r.within_cap and r.alternative and r.alternative.within_cap, ctx
            else:
                assert r.cap_cost_mid <= FOSSIL.cost_cap_gbp, ctx
        if r.exempt_reason == "over_cap":
            assert r.reaches_c and r.cost_known and r.cap_cost_mid > FOSSIL.cost_cap_gbp, ctx
            assert not (r.alternative and r.alternative.within_cap), ctx
        if r.exempt_reason == "cannot_reach_c":
            assert not r.reaches_c, ctx
        if not r.cost_known:
            assert r.confidence == "low" and not r.within_cap, ctx
        if r.borderline:
            assert not r.score_exact and 69 <= r.final_score < 71, ctx
        # money adds up
        assert r.cost_low <= r.cost_mid <= r.cost_high, ctx
        assert r.cap_cost_mid <= r.cost_mid + 1e-9, ctx
        assert r.cap_cost_mid == pytest.approx(
            sum(s.cost_mid or 0 for s in r.steps if s.counts_toward_cap)
        ), ctx
        # route is well formed
        assert r.final_score == r.current_score + sum(s.uplift for s in r.steps), ctx
        assert len({s.sequence for s in r.steps}) == len(r.steps), ctx
        main_seqs = {s.sequence for s in steps if s.category != "3"}
        assert {s.sequence for s in r.steps} <= main_seqs, ctx
        assert all(s.uplift > 0 for s in r.steps), ctx
        if not r.below_c:
            assert r.steps == () and not r.lock_in_candidate, ctx
        # the route is the cheapest, preferring no fossil heating (independent brute force)
        main_steps = [s for s in steps if s.category != "3"]
        if r.below_c and r.reaches_c and r.cost_known:
            clean = _brute_force_best(current, main_steps, FOSSIL, allow_fossil=False)
            best = clean or _brute_force_best(current, main_steps, FOSSIL, allow_fossil=True)
            assert best is not None, ctx
            assert (r.cap_cost_mid > FOSSIL.cost_cap_gbp, r.cost_mid) == pytest.approx(best), ctx
            if clean:
                assert all(s.counts_toward_cap for s in r.steps), ctx
            if r.alternative:
                assert any(not s.counts_toward_cap for s in r.alternative.steps), ctx
                assert (
                    r.alternative.cost_mid < r.cost_mid
                    or r.alternative.within_cap
                    and not r.within_cap
                ), ctx
        if r.safer:
            assert r.borderline and r.safer.final_score >= 71, ctx
            assert r.safer.cost_mid >= r.cost_mid - 1e-9, ctx
