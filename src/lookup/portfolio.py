"""Resolve a landlord's portfolio CSV to UPRNs.

CSV columns (header row required, case-insensitive):
  address   required unless uprn is given, e.g. "Flat 2, 15 Example Road, E13 0AA"
  postcode  optional if the address already contains it
  label     optional, the landlord's own name for the property (shown in the report)
  uprn      optional; skips matching. Use it to settle an "ambiguous" row.

The CSV is the landlord's own data: keep it out of the repo (data/portfolios/ is ignored).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from lookup.address_match import (
    Candidate,
    MatchResult,
    match_address,
    normalise_postcode,
    split_postcode,
)


@dataclass(frozen=True)
class PortfolioRow:
    line: int  # CSV line number, for messages back to the landlord
    address: str
    postcode: str | None
    label: str
    uprn: int | None


@dataclass
class Resolved:
    row: PortfolioRow
    status: str  # MatchResult.status, plus "given" (uprn column) and "duplicate"
    uprn: int | None = None
    # For "ambiguous": (uprn, register address) pairs the landlord can choose from.
    options: list[tuple[int, str]] = field(default_factory=list)


def read_portfolio_csv(path: str | Path) -> list[PortfolioRow]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("portfolio CSV is empty")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        if "address" not in cols and "uprn" not in cols:
            raise ValueError("portfolio CSV needs an 'address' or 'uprn' column")

        def get(rec: dict, name: str) -> str:
            return (rec.get(cols[name]) or "").strip() if name in cols else ""

        rows = []
        for line, rec in enumerate(reader, start=2):
            address, uprn = get(rec, "address"), get(rec, "uprn")
            if not address and not uprn:
                continue  # blank line
            if uprn and not uprn.isdigit():
                raise ValueError(f"line {line}: uprn must be a number, got {uprn!r}")
            rows.append(
                PortfolioRow(
                    line=line,
                    address=address,
                    postcode=normalise_postcode(get(rec, "postcode")),
                    label=get(rec, "label") or address or f"UPRN {uprn}",
                    uprn=int(uprn) if uprn else None,
                )
            )
    return rows


def _row_postcode(row: PortfolioRow) -> str | None:
    return row.postcode or split_postcode(row.address)[1]


def load_candidates(con: duckdb.DuckDBPyConnection, postcodes: list[str]) -> list[Candidate]:
    """Every register address variant in the given postcodes (from the certificate index)."""
    if not postcodes:
        return []
    rows = con.execute(
        """
        select distinct uprn, postcode,
            concat_ws(', ', address_line1, address_line2, address_line3, address_line4)
        from raw.epc_certificate_index
        where uprn is not null and upper(trim(postcode)) in (select unnest(?))
        """,
        [postcodes],
    ).fetchall()
    out = []
    for uprn, pc, addr in rows:
        npc = normalise_postcode(pc)
        if npc:
            out.append(Candidate(int(uprn), npc, addr))
    return out


def resolve(rows: list[PortfolioRow], candidates: list[Candidate]) -> list[Resolved]:
    """Match each row; the first row for a UPRN wins, later ones are marked duplicate."""
    addr_by_uprn: dict[int, str] = {}
    for c in candidates:  # one display address per UPRN (longest variant: most specific)
        if len(c.address) > len(addr_by_uprn.get(c.uprn, "")):
            addr_by_uprn[c.uprn] = c.address

    seen: set[int] = set()
    out = []
    for row in rows:
        if row.uprn is not None:
            res = MatchResult("given", row.uprn)
        else:
            res = match_address(row.address, candidates, postcode=row.postcode)
        r = Resolved(row, res.status, res.uprn)
        if res.status == "ambiguous":
            r.options = [(u, addr_by_uprn.get(u, "")) for u in res.alternatives]
        if r.uprn is not None:
            if r.uprn in seen:
                r.status = "duplicate"
            seen.add(r.uprn)
        out.append(r)
    return out


def resolve_csv(con: duckdb.DuckDBPyConnection, path: str | Path) -> list[Resolved]:
    rows = read_portfolio_csv(path)
    postcodes = sorted({pc for r in rows if (pc := _row_postcode(r))})
    return resolve(rows, load_candidates(con, postcodes))
