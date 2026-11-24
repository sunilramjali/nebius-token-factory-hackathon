"""Synthetic addresses only — no register data in the repo (ADR-007)."""

import pytest

from lookup.address_match import Candidate, match_address, normalise_postcode, tokens
from lookup.match_rate import typed_variants

PC = "E99 9ZZ"
CANDS = [
    Candidate(1, PC, "12 Example Road"),
    Candidate(2, PC, "12a Example Road"),
    Candidate(3, PC, "Flat 3, Sample House, 26 Example Road"),
    Candidate(4, PC, "Flat 26, Sample House, 3 Example Road"),
    Candidate(5, PC, "Flat B, 40 Example Road"),
    Candidate(6, PC, "40 Example Road"),
    Candidate(7, PC, "First Floor Flat, 50 Example Road"),
    Candidate(8, PC, "Ground Floor Flat, 50 Example Road"),
    Candidate(9, PC, "7, St. Johns Street"),
    Candidate(10, "E98 8YY", "12 Example Road"),
    Candidate(11, PC, "Apartment 10, 60 Example Road"),
    Candidate(12, PC, "Apartment 1, 60 Example Road"),
    Candidate(13, PC, "Flat 7, Sample House, 1a Example Road"),
    Candidate(14, PC, "7 Example Road"),
    Candidate(15, PC, "Apartment 60, 60 Example Road"),
]


@pytest.mark.parametrize(
    "raw, expected",
    [("e99 9zz", "E99 9ZZ"), ("E999ZZ", "E99 9ZZ"), (" e6 1fl ", "E6 1FL"), ("nope", None)],
)
def test_normalise_postcode(raw, expected):
    assert normalise_postcode(raw) == expected


def test_tokens_expand_abbreviations_and_unit_identifiers():
    assert tokens("12 Example Rd") == ["12", "example", "road"]
    assert tokens("11 A Upton Ln") == ["11a", "upton", "lane"]
    assert tokens("Flat B, 40 Example Road") == ["#flat-b", "40", "example", "road"]
    assert tokens("First Floor Flat, 50 High St") == ["#floor-1", "50", "high", "st"]
    assert tokens("16/267 Barking Rd") == ["16", "267", "barking", "road"]


@pytest.mark.parametrize(
    "text, uprn",
    [
        ("12 example rd e999zz", 1),
        ("12A Example Road, E99 9ZZ", 2),
        ("Flat 3, 26 Example Rd, E99 9ZZ", 3),  # building name dropped
        ("flat 26 sample house 3 example road e99 9zz", 4),  # number order matters
        ("Flat B 40 Example Road E99 9ZZ", 5),
        ("First Floor Flat 50 Example Rd E99 9ZZ", 7),
        ("7 Saint Johns St E99 9ZZ", 9),
        ("Apartment 10, 60 Example Road, E99 9ZZ", 11),
        ("Apt 60, 60 Example Road, E99 9ZZ", 15),  # not Apartment 1 / 10 (multiset)
        ("7 Example Road E99 9ZZ", 14),  # Flat 7 at no. 1a is another building
    ],
)
def test_match_picks_right_uprn(text, uprn):
    res = match_address(text, CANDS)
    assert (res.status, res.uprn) == ("matched", uprn)


def test_separate_postcode_argument():
    assert match_address("12 Example Road", CANDS, postcode="e98 8yy").uprn == 10


def test_failure_statuses():
    assert match_address("12 Example Road", CANDS).status == "no_postcode"
    assert match_address("12 Example Road E11 1AA", CANDS).status == "no_candidates"
    assert match_address("99 Example Road E99 9ZZ", CANDS).status == "no_match"
    # Two flats at no. 50 and the query names neither → don't guess.
    assert match_address("50 Example Road E99 9ZZ", CANDS).status == "ambiguous"
    # Whole-building record beside a flat at no. 40 → still ask which one.
    res = match_address("40 Example Road E99 9ZZ", CANDS)
    assert (res.status, res.alternatives) == ("ambiguous", (5, 6))
    # No. 26 is a block (Flat 3 lives there): the street address alone isn't enough.
    assert match_address("26 Example Road E99 9ZZ", CANDS).status == "ambiguous"
    assert match_address("60 Example Road E99 9ZZ", CANDS).status == "ambiguous"


def test_typed_variants_keep_unit_and_drop_building():
    v = typed_variants(["Flat B", "Sample House", "40 Example Road", "Somewhere"], PC)
    assert v["exact"] == "Flat B, Sample House, 40 Example Road, Somewhere, E99 9ZZ"
    assert v["no_building_name"] == "Flat B, 40 Example Road E99 9ZZ"
    assert v["terse"] == "flat b, 40 example rd, e999zz"
    for text in v.values():
        assert match_address(text, CANDS).uprn == 5


def test_unit_omitted_strips_flat_identifier():
    from lookup.match_rate import unit_omitted

    assert unit_omitted(["Flat 3", "Sample House", "26 Example Road"]) == "26 Example Road"
    assert unit_omitted(["Flat 23, Sample Court, 26 Example Road", "Town"]) == "26 Example Road"
    assert unit_omitted(["Flat B, 40 Example Road"]) == "40 Example Road"
    assert unit_omitted(["Flat 8", "3, Example Road"]) == "3, Example Road"
