"""Export the 6 demo homes' engine results to examples/demo_portfolio.json for the hosted demo.

    uv run python scripts/export_demo.py

The file holds UPRNs and EPC/engine fields only (Open Government Licence), never address or
postcode fields (docs/data_access.md §3), so it can be committed and served publicly.
"""

from __future__ import annotations

import json
import os
import re

import duckdb

from report.portfolio_report import load_properties
from webapp.logic import DEMO_FILE, DEMO_PORTFOLIO_SQL, dump_demo

POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b", re.I)


def main() -> None:
    con = duckdb.connect(os.environ.get("DUCKDB_TARGET", "data/epc.duckdb"), read_only=True)
    labels = {str(uprn): label for label, uprn in con.execute(DEMO_PORTFOLIO_SQL).fetchall()}
    props, missing = load_properties(con, list(labels), labels, None, strict=True)
    data = dump_demo(props)
    out = json.dumps(data, indent=1, ensure_ascii=False)
    if POSTCODE.search(out):  # belt and braces: nothing address-like may leave the laptop
        raise SystemExit("refusing to write: a postcode-like string is in the export")
    DEMO_FILE.write_text(out + "\n")
    print(f"wrote {DEMO_FILE} ({len(props)} homes)")


if __name__ == "__main__":
    main()
