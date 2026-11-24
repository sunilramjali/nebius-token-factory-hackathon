"""Match a landlord-typed address to a UPRN from the EPC register.

Strategy (deterministic, stdlib only):
  1. Narrow by postcode (exact after normalisation). Postcodes in Newham hold a median
     of 26 UPRNs, max ~160, so the candidate set is small.
  2. Score each candidate UPRN on its address variants:
       - identifiers (house / flat numbers incl. "11a", "Flat B", "First Floor") must
         match as a set.
         A wrong number is a wrong property, so this is a hard filter;
       - ordered numbers break the "Flat 3, 26 X St" vs "Flat 26, 3 X St" tie;
       - word-token overlap (after abbreviation expansion) ranks the rest.
  3. Return the best UPRN, or no match / ambiguous when it can't decide.

Addresses are display-only under the register's OS / Royal Mail terms (ADR-007): this
module never stores them, it only compares strings passed in by the caller.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

# Expansion of common abbreviations. "st" stays "st" (street vs saint is ambiguous),
# and "street"/"saint" collapse onto it.
_ABBREV = {
    "rd": "road",
    "st": "st",
    "street": "st",
    "saint": "st",
    "ave": "avenue",
    "av": "avenue",
    "gdns": "gardens",
    "gr": "grove",
    "gro": "grove",
    "ln": "lane",
    "pl": "place",
    "sq": "square",
    "cres": "crescent",
    "cl": "close",
    "ct": "court",
    "dr": "drive",
    "terr": "terrace",
    "tce": "terrace",
    "hse": "house",
    "apartment": "flat",
    "apt": "flat",
    "maisonette": "flat",
    "unit": "flat",
}
# Words that carry no identifying information.
_NOISE = {"flat", "floor", "london", "the", "and", "of"}
# "First Floor Flat" etc. identify a unit just like a number does.
_FLOORS = {
    "basement": "b", "lower": "l", "ground": "0", "first": "1", "second": "2",
    "third": "3", "fourth": "4", "top": "t", "upper": "u",
}  # fmt: skip

_POSTCODE_RE = re.compile(r"\b([a-z]{1,2}\d[a-z\d]?)\s*(\d[a-z]{2})\b", re.I)
_NUMBER_RE = re.compile(r"^\d+[a-z]?$")


def normalise_postcode(postcode: str | None) -> str | None:
    """'e16 4au' / 'E164AU' → 'E16 4AU'. None if it doesn't look like a UK postcode."""
    if not postcode:
        return None
    m = _POSTCODE_RE.search(postcode.strip())
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}".upper()


def split_postcode(text: str) -> tuple[str, str | None]:
    """Pull a postcode out of free text; returns (text without postcode, postcode)."""
    m = _POSTCODE_RE.search(text)
    if not m:
        return text, None
    return (text[: m.start()] + " " + text[m.end() :]), normalise_postcode(m.group(0))


def tokens(text: str) -> list[str]:
    """Lowercase, split '11a' style numbers from words, expand abbreviations, drop noise."""
    text = text.lower().replace("'", "")
    text = re.sub(r"(\d+)\s*/\s*(\d+)", r"\1 \2", text)  # "16/267" → "16 267"
    text = re.sub(r"(\d+)\s+([a-z])\b", r"\1\2", text)  # "11 a" → "11a"
    raw = [_ABBREV.get(t, t) for t in re.findall(r"\d+[a-z]?(?![a-z])|[a-z]+", text)]
    out = []
    for i, t in enumerate(raw):
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        prev = raw[i - 1] if i else ""
        if t == "flat" and len(nxt) == 1 and nxt.isalpha():
            out.append(f"#flat-{nxt}")  # "Flat B" → unit identifier
        elif len(t) == 1 and t.isalpha() and prev == "flat":
            continue  # consumed above
        elif t in _FLOORS and nxt == "floor":
            out.append(f"#floor-{_FLOORS[t]}")  # "First Floor Flat" → unit identifier
        elif t not in _NOISE:
            out.append(t)
    return out


def _is_id(t: str) -> bool:
    return bool(_NUMBER_RE.match(t)) or t.startswith("#")


def _numbers(toks: list[str]) -> list[str]:
    """Identifying tokens: house/flat numbers, flat letters, floor markers."""
    return [t for t in toks if _is_id(t)]


def _street_number(toks: list[str]) -> str | None:
    """The building's street number: the last plain number (unit markers excluded)."""
    nums = [t for t in toks if _NUMBER_RE.match(t)]
    return nums[-1] if nums else None


def _words(toks: list[str]) -> set[str]:
    return {t for t in toks if not _is_id(t)}


@dataclass(frozen=True)
class Candidate:
    uprn: int
    postcode: str  # normalised
    address: str  # one register address variant (lines joined)


@dataclass(frozen=True)
class MatchResult:
    # "matched" | "ambiguous" (several units fit: ask which) | "no_postcode"
    # | "no_candidates" (postcode not in register) | "no_match"
    status: str
    uprn: int | None = None
    score: float = 0.0
    alternatives: tuple[int, ...] = ()  # for "ambiguous": the UPRNs to choose between


def _score(query_toks: list[str], cand_toks: list[str]) -> float | None:
    qn, cn = _numbers(query_toks), _numbers(cand_toks)
    if Counter(qn) != Counter(cn):  # multiset: "Apartment 10, 10 X Ave" keeps both 10s
        return None
    qw, cw = _words(query_toks), _words(cand_toks)
    word = len(qw & cw) / len(qw | cw) if (qw | cw) else 1.0
    # Order bonus only matters when several numbers are present.
    order = 0.5 if qn == cn and len(qn) > 1 else 0.0
    # Containment: landlords often drop the building name, so reward query ⊆ candidate.
    contained = 0.25 if qw <= cw else 0.0
    return 1.0 + order + contained + word


def match_address(
    text: str, candidates: Iterable[Candidate], postcode: str | None = None
) -> MatchResult:
    """Match free text (optionally with a separate postcode) against register candidates.

    `candidates` may contain several variants per UPRN; the best variant counts.
    """
    body, found_pc = split_postcode(text)
    pc = normalise_postcode(postcode) or found_pc
    if not pc:
        return MatchResult("no_postcode")

    in_pc = [c for c in candidates if c.postcode == pc]
    if not in_pc:
        return MatchResult("no_candidates")

    q = tokens(body)
    q_ids, q_words = Counter(_numbers(q)), _words(q)
    q_street_no = _street_number(q)
    best: dict[int, float] = {}
    other_units: set[int] = set()  # UPRNs at the same address with extra unit identifiers
    for c in in_pc:
        ct = tokens(c.address)
        s = _score(q, ct)
        if s is not None:
            best[c.uprn] = max(s, best.get(c.uprn, -1.0))
        elif (
            q_ids
            and q_ids < Counter(_numbers(ct))
            and _street_number(ct) == q_street_no  # same building, not "Flat 7, 1a X Walk"
            and q_words <= _words(ct)
        ):
            other_units.add(c.uprn)  # e.g. "Flat B, 40 X Road" when the query is "40 X Road"

    # The query names a building but not which unit in it: ask, don't guess. This also
    # covers an old whole-building record ("40 X Road") that sits beside its flats.
    if other_units - best.keys():
        return MatchResult("ambiguous", alternatives=tuple(sorted(other_units | best.keys())))
    if not best:
        return MatchResult("no_match")

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    top_uprn, top = ranked[0]
    if len(ranked) > 1 and abs(ranked[1][1] - top) < 1e-9:
        tied = tuple(sorted(u for u, sc in ranked if abs(sc - top) < 1e-9))
        return MatchResult("ambiguous", score=top, alternatives=tied)
    return MatchResult("matched", top_uprn, top)
