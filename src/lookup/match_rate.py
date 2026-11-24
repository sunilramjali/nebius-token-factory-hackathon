"""Measure the address → UPRN match rate on a sample of register properties.

    uv run python -m lookup.match_rate --n 50 --seed 42

For each sampled UPRN we take its latest register address and generate several
"landlord-typed" variants (abbreviated, lowercase, building name dropped, ...), then
match each against every register address in the same postcode.

Prints aggregate rates only. Addresses are never printed or written (ADR-007).
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict

from ingest.warehouse import connect
from lookup.address_match import Candidate, match_address, normalise_postcode

_ABBREVIATE = [
    (r"\bRoad\b", "Rd"),
    (r"\bStreet\b", "St"),
    (r"\bAvenue\b", "Ave"),
    (r"\bGardens\b", "Gdns"),
    (r"\bLane\b", "Ln"),
    (r"\bClose\b", "Cl"),
    (r"\bCourt\b", "Ct"),
]


def _abbreviate(s: str) -> str:
    for pat, rep in _ABBREVIATE:
        s = re.sub(pat, rep, s, flags=re.I)
    return s


def typed_variants(lines: list[str], postcode: str) -> dict[str, str]:
    """Plausible ways a landlord might type the same address."""
    lines = [ln.strip() for ln in lines if ln and ln.strip()]
    pc_nospace = postcode.replace(" ", "").lower()
    # Drop building / locality names, keep anything that identifies the unit.
    unit = re.compile(r"\d|\b(flat|apartment|floor|basement)\b", re.I)
    with_numbers = [ln for ln in lines if unit.search(ln)] or lines
    terse = re.sub(r"\b(flat|apartment)\s+(?=\d)", "", ", ".join(with_numbers), flags=re.I)
    return {
        "exact": f"{', '.join(lines)}, {postcode}",
        "abbrev_lower": f"{_abbreviate(' '.join(lines)).lower()} {pc_nospace}",
        "no_building_name": f"{', '.join(with_numbers)} {postcode}",
        "terse": f"{_abbreviate(terse).lower()}, {pc_nospace}",
    }


def unit_omitted(lines: list[str]) -> str:
    """Street address only, e.g. 'Flat 3, X House, 26 Y Road' → '26 Y Road'."""
    street = [ln for ln in lines if re.search(r"\d+\w*,?\s+[a-z]{3,}", ln, re.I)]
    last = street[-1] if street else lines[-1]
    # "Flat 23, X Court, 26 High Street" can sit in one line: keep from the last part that
    # starts with a number ("3, Deanery Road" stays whole).
    parts = re.split(r",\s*", last)
    starts = [i for i, p in enumerate(parts) if re.match(r"\d", p)]
    return ", ".join(parts[starts[-1] :]) if starts else last


def run(n: int, seed: int) -> None:
    con = connect()
    sample = con.execute(
        f"""
        with latest as (
            select *, row_number() over (
                partition by uprn order by registration_date desc, certificate_number desc
            ) rn
            from raw.epc_certificate_index where uprn is not null and postcode is not null
        )
        select uprn, address_line1, address_line2, address_line3, address_line4, postcode
        from latest where rn = 1
        order by hash(uprn, {int(seed)})  -- deterministic sample for a given seed
        limit {int(n)}
        """
    ).fetchall()
    postcodes = sorted({r[5] for r in sample})
    cand_rows = con.execute(
        """
        select distinct uprn, postcode,
            concat_ws(', ', address_line1, address_line2, address_line3, address_line4)
        from raw.epc_certificate_index
        where uprn is not null and postcode in (select unnest(?))
        """,
        [postcodes],
    ).fetchall()
    by_pc: dict[str, list[Candidate]] = defaultdict(list)
    for uprn, pc, addr in cand_rows:
        npc = normalise_postcode(pc)
        if npc:
            by_pc[npc].append(Candidate(uprn, npc, addr))

    coverage = con.execute("select count(*), count(uprn) from raw.epc_certificate_index").fetchone()

    outcomes: dict[str, Counter] = defaultdict(Counter)
    for uprn, l1, l2, l3, l4, pc in sample:
        lines = [x for x in (l1, l2, l3, l4) if x]
        joined = " ".join(lines)
        is_flat = re.search(r"\b(flat|apartment|floor|maisonette)\b", joined, re.I)
        kind = "flat" if is_flat or len(re.findall(r"\d+", joined)) > 1 else "house"
        for name, text in typed_variants(lines, pc).items():
            res = match_address(text, by_pc.get(normalise_postcode(pc) or "", []))
            if res.status == "matched":
                outcome = "correct" if res.uprn == uprn else "wrong"
            else:
                outcome = res.status
            outcomes[name][outcome] += 1
            outcomes[f"{name} [{kind}]"][outcome] += 1
            outcomes["ALL"][outcome] += 1
        if kind == "flat":
            # Stress case: landlord types only the street address of a flat. The only
            # safe answers are "ambiguous" or (if the register has no other unit) correct.
            text = f"{unit_omitted(lines)} {pc}"
            res = match_address(text, by_pc.get(normalise_postcode(pc) or "", []))
            ok = res.status != "matched" or res.uprn == uprn
            outcome = res.status if res.status != "matched" else ("correct" if ok else "wrong")
            outcomes["STRESS unit_omitted [flat]"][outcome] += 1

    total, with_uprn = coverage
    print(f"UPRN coverage: {with_uprn:,}/{total:,} certificates ({with_uprn / total:.1%})")
    print(f"Sample: {len(sample)} UPRNs in {len(postcodes)} postcodes (seed {seed})\n")
    cols = ["correct", "wrong", "ambiguous", "no_match", "no_candidates", "no_postcode"]
    print(f"{'variant':32} {'n':>4} " + " ".join(f"{c:>9}" for c in cols) + "  correct%")
    for name in sorted(outcomes, key=lambda k: (k.startswith("STRESS"), k == "ALL", k)):
        c = outcomes[name]
        tot = sum(c.values())
        print(
            f"{name:32} {tot:>4} "
            + " ".join(f"{c[k]:>9}" for k in cols)
            + f"  {c['correct'] / tot:>7.1%}"
        )


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    run(args.n, args.seed)


if __name__ == "__main__":
    main()
