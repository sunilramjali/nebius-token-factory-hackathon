"""Copy the tables the app needs from MotherDuck into a local DuckDB file.

Run once before the hackathon so the demo never depends on MotherDuck:
    uv run --env-file .env python scripts/snapshot.py

The output (data/epc.duckdb) holds register address fields, so it is gitignored and
must not be shared (EPC address licence, see docs/data_access.md §3).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

TABLES = [
    "raw.epc_certificate_index",
    "raw.epc_certificate_documents",
    "marts.fct_property_compliance",
    "marts.fct_recommendations_for_engine",
    "analytics.route_results",
    "analytics.route_steps",
    "analytics.predictions",
    "seeds.epc_codes",
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", default="md:route_to_c")
    p.add_argument("--out", default="data/epc.duckdb")
    args = p.parse_args()

    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} already exists; delete it first to refresh.")
    out.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(args.source)
    con.execute(f"ATTACH '{out}' AS snap")
    for table in TABLES:
        schema = table.split(".")[0]
        con.execute(f"CREATE SCHEMA IF NOT EXISTS snap.{schema}")
        con.execute(f"CREATE TABLE snap.{table} AS SELECT * FROM {table}")
        n = con.execute(f"SELECT count(*) FROM snap.{table}").fetchone()[0]
        print(f"{table}: {n:,} rows")
    con.execute("DETACH snap")


if __name__ == "__main__":
    main()
