from datetime import date

from engine.policy import Policy
from report.portfolio_report import SAMPLE_NOTE, Property, Step, next_actions, render, status

POLICY = Policy(69, 10_000, date(2029, 9, 30), 10)
TODAY = date(2026, 11, 6)


def prop(label, lock, exempt, lo, hi, steps=()):
    return Property(
        property_id=label, label=label, property_type="house",
        registration_date=date(2023, 6, 1), current_score=60, current_band="D",
        final_score=71, reaches_c=True, cost_low=lo, cost_high=hi, cost_mid=(lo + hi) / 2,
        within_cap=not exempt, confidence="high", lock_in_candidate=lock,
        likely_exempt_cost_cap=exempt, needs_survey=False, notes="", steps=list(steps),
        exempt_reason="over_cap" if exempt else None,
    )  # fmt: skip


def card(p, policy=POLICY):
    out = render([p], policy, "T", TODAY)
    return out[out.index("Property by property") :]


def test_render_has_disclaimer_licence_actions_and_order():
    cheap = prop("Cheap <home>", True, False, 400, 950,
                 [Step(1, "Loft insulation", 8, 100, 350, 68, "D")])  # fmt: skip
    pricey = prop("Pricey", False, True, 6000, 17000)
    out = render([pricey, cheap], POLICY, "Test", TODAY)

    assert "a survey by an accredited energy assessor is required" in out
    assert "Open Government Licence v3.0" in out
    assert "Cheap &lt;home&gt;" in out  # escaped
    assert out.index("Cheap &lt;home&gt;") < out.index("Pricey")  # fix-early first
    assert "What to do next" in out
    assert out.index("What to do next") < out.index("Your homes")
    assert out.count("should count as compliant until it expires") == 1
    assert "register an exemption" in out
    # spend up to the cap: 400 + min(6000, 10000) .. 950 + min(17000, 10000)
    assert "£6,400–£10,950" in out


def test_status_labels_cover_every_case():
    fix = prop("F", True, False, 400, 950)
    border = prop("B", True, False, 400, 950)
    border.borderline = True
    boiler = prop("X", True, False, 10500, 10500)
    boiler.lock_in_needs_boiler, boiler.within_cap = True, False
    quotes = prop("Q", True, False, 6000, 14000)
    quotes.cap_verdict_uncertain = True
    over = prop("O", False, True, 9000, 20000)
    cannot = prop("C", False, True, 2000, 2000)
    cannot.exempt_reason, cannot.reaches_c = "cannot_reach_c", False
    survey = prop("S", False, False, 0, 0)
    survey.cost_known = False
    done = prop("D", False, False, 0, 0)
    done.current_score = 72
    got = {p.label: (status(p, POLICY).key, status(p, POLICY).colour)
           for p in [fix, border, boiler, quotes, over, cannot, survey, done]}  # fmt: skip
    assert got == {
        "F": ("fix", "green"), "B": ("borderline", "amber"), "X": ("boiler", "amber"),
        "Q": ("quotes", "amber"), "O": ("over_cap", "red"), "C": ("cannot", "red"),
        "S": ("survey", "grey"), "D": ("done", "green"),
    }  # fmt: skip


def test_next_actions_ordered_grouped_and_capped():
    homes = [prop(f"Home {i}", True, False, 400, 950) for i in range(5)]
    over = prop("Big", False, True, 9000, 20000)
    acts = next_actions([over, *homes], POLICY)
    assert acts[0].startswith("Get quotes for Home 0, Home 1, Home 2 and 2 more.")
    assert "30 September 2029" in acts[0]
    assert "Big" in acts[1] and "exemption may be possible" in acts[1]
    assert len(acts) <= 5


def test_cost_unknown_is_never_fix_early():
    p = prop("U", False, False, 0, 0)
    p.cost_known, p.within_cap = False, False
    out = card(p)
    assert "Survey first" in out and "Fix early" not in out
    summary = render([p], POLICY, "T", TODAY)
    assert "0 of 1 homes with known costs" in summary and "Book a survey for U" in summary


def test_borderline_route_is_labelled():
    p = prop("B", True, False, 400, 950)
    p.final_score, p.borderline, p.score_exact = 69, True, False
    out = render([p], POLICY, "T", TODAY)
    assert "Fix early (borderline)" in out and "(estimate)" in out
    assert "only just reaches C (69, an estimate)" in out


def test_over_cap_wording_and_straddle_note():
    p = prop("O", False, True, 6000, 17000)
    p.cap_verdict_uncertain = True
    out = card(p)
    assert "Likely over the cap: about £6,000–£17,000" in out
    assert "real quotes will decide" in out
    assert "exemption process for the 2030 rules isn't published yet" in out


def test_fossil_boiler_marked_and_counted_part_shown():
    boiler = Step(1, "Replace boiler", 5, 2200, 3000, 66, "D", counts_toward_cap=False)
    p = prop("F", True, False, 6220, 17000, [boiler])
    p.cap_cost_low, p.cap_cost_high = 4020, 14000
    out = card(p)
    assert "(doesn't count toward the cap)" in out
    assert "£4,020–£14,000 of this counts toward the £10,000 cap" in out
    assert "includes a new gas or oil boiler" in out


def test_flat_shared_fabric_note_only_for_flats():
    wall = Step(1, "Wall insulation", 6, 4000, 14000, 72, "C", improvement_type="Q")
    flat = prop("Flat", True, False, 4000, 14000, [wall])
    flat.property_type = "flat"
    house = prop("House", True, False, 4000, 14000, [wall])
    assert "freeholder" in card(flat)
    assert "freeholder" not in card(house)


def test_fix_early_mentions_new_epc_cost_and_plain_confidence():
    out = card(prop("L", True, False, 400, 950))
    assert "Costs exclude the new EPC" in out
    assert "recent certificate, full cost data" in out and "data confidence" not in out


def test_safer_route_and_boiler_alternative_rendered():
    p = prop("S", True, False, 400, 950)
    p.final_score, p.borderline, p.score_exact = 69, True, False
    p.safer = [Step(1, "Loft", 4, 100, 350, 65, "D"), Step(2, "Walls", 7, 1000, 2000, 72, "C")]
    p.alternative = [Step(1, "Boiler", 9, 200, 300, 69, "C", counts_toward_cap=False)]
    out = card(p)
    assert "Safer route: about £1,100–£2,350" in out and "aims for 72" in out
    assert "Cheaper option with a boiler" in out and "£0 of this counts toward" in out
    assert "costs as of 2023" in out


def test_boiler_option_flagged_when_it_is_the_only_one_within_cap():
    p = prop("X", False, True, 10000, 12000)
    p.within_cap = False
    p.alternative = [Step(1, "Boiler", 9, 3000, 3000, 69, "C", counts_toward_cap=False),
                     Step(2, "Floor", 4, 7000, 9000, 73, "C")]  # fmt: skip
    out = card(p)
    assert "Option with a boiler" in out
    assert "only option likely within the £10,000 cap" in out


def test_lock_in_only_with_boiler_wording():
    p = prop("NB", True, False, 10500, 10500)
    p.within_cap, p.lock_in_needs_boiler = False, True
    p.alternative = [Step(1, "Boiler", 6, 3000, 3000, 65, "D", counts_toward_cap=False),
                     Step(2, "Floor", 4, 8000, 8000, 69, "C")]  # fmt: skip
    out = render([p], POLICY, "T", TODAY)
    assert "Fix early (needs a boiler)" in out and "Decide on heating for NB" in out
    assert "£11,000–£11,000" in out  # spend follows the within-cap boiler route


def test_sample_note_shown_only_when_given():
    p = prop("A", True, False, 400, 950)
    assert "not typical" not in render([p], POLICY, "T", TODAY)
    assert "not typical" in render([p], POLICY, "T", TODAY, note=SAMPLE_NOTE)


def test_print_layout_keeps_headings_with_content():
    out = render([prop("A", True, False, 400, 950), prop("B", True, False, 400, 950)],
                 POLICY, "T", TODAY)  # fmt: skip
    # heading wrapped with the first card, details start on a new printed page
    assert "<div class='keep'><h2>Property by property</h2><section class=\"card\">" in out
    assert ".details {{".replace("{{", "{") in out and "break-before: page" in out
    assert "break-after: avoid" in out and "break-inside: avoid" in out
    assert '<section class="keep"><h2>Your homes</h2>' in out
