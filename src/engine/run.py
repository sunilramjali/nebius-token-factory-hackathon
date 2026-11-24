"""Run the route-to-C engine over every property and persist the results.

Reads (dbt marts):  marts.fct_property_compliance, marts.fct_recommendations_for_engine
Writes (schema analytics, owned by the engine — ADR-004):
  route_results  one row per property for the latest run (replaced each run)
  route_steps    one row per route / further-measure step for the latest run (replaced)
  predictions    append-only history of every run, for calibration later (ADR-005)

Usage: python -m engine.run [--target md:route_to_c] [--as-of 2026-11-06]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import tempfile
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from dotenv import load_dotenv

from engine import __version__
from engine.policy import Policy, load_policy
from engine.route_to_c import Certificate, RouteResult, RouteSummary, Step, route_to_c

log = logging.getLogger("engine.run")

# route_results / route_steps hold only the latest run, so they are dropped and recreated each
# run (schema changes need no migration). predictions is append-only and never dropped.
DDL = """
CREATE SCHEMA IF NOT EXISTS analytics;
DROP TABLE IF EXISTS analytics.route_results;
DROP TABLE IF EXISTS analytics.route_steps;
CREATE TABLE analytics.route_results (
    run_id VARCHAR, engine_version VARCHAR, as_of DATE, created_at TIMESTAMP,
    property_id VARCHAR, uprn BIGINT, certificate_number VARCHAR,
    current_score INTEGER, current_band VARCHAR, below_c BOOLEAN, gap_to_c INTEGER,
    final_score INTEGER, reaches_c BOOLEAN, n_route_steps INTEGER,
    cost_low DOUBLE, cost_high DOUBLE, cost_mid DOUBLE,
    within_cap BOOLEAN, within_cap_worst_case BOOLEAN,
    cert_age_years DOUBLE, is_stale BOOLEAN, confidence VARCHAR,
    likely_exempt_cost_cap BOOLEAN, lock_in_candidate BOOLEAN, needs_survey BOOLEAN,
    notes VARCHAR,
    cap_cost_low DOUBLE, cap_cost_high DOUBLE, cap_cost_mid DOUBLE, cost_known BOOLEAN,
    score_exact BOOLEAN, borderline BOOLEAN, cap_verdict_uncertain BOOLEAN,
    exempt_reason VARCHAR,
    alt_cost_low DOUBLE, alt_cost_high DOUBLE, alt_cost_mid DOUBLE, alt_cap_cost_mid DOUBLE,
    alt_final_score INTEGER, alt_within_cap BOOLEAN,
    safer_cost_low DOUBLE, safer_cost_high DOUBLE, safer_cost_mid DOUBLE,
    safer_cap_cost_mid DOUBLE, safer_final_score INTEGER, safer_within_cap BOOLEAN,
    lock_in_needs_boiler BOOLEAN
);
CREATE TABLE analytics.route_steps (
    run_id VARCHAR, property_id VARCHAR, certificate_number VARCHAR,
    step_type VARCHAR, step_order INTEGER, sequence INTEGER, improvement_type VARCHAR,
    uplift INTEGER, cost_low DOUBLE, cost_high DOUBLE, cost_mid DOUBLE,
    cumulative_score INTEGER, cumulative_band VARCHAR,
    cumulative_cost_low DOUBLE, cumulative_cost_high DOUBLE, cumulative_cost_mid DOUBLE,
    counts_toward_cap BOOLEAN
);
CREATE TABLE IF NOT EXISTS analytics.predictions (
    run_id VARCHAR, engine_version VARCHAR, as_of DATE, created_at TIMESTAMP,
    property_id VARCHAR, uprn BIGINT, certificate_number VARCHAR,
    current_score INTEGER, predicted_final_score INTEGER, reaches_c BOOLEAN,
    cost_low DOUBLE, cost_high DOUBLE, cost_mid DOUBLE, confidence VARCHAR,
    route JSON
);
"""

RESULT_COLS = [
    "run_id", "engine_version", "as_of", "created_at", "property_id", "uprn",
    "certificate_number", "current_score", "current_band", "below_c", "gap_to_c",
    "final_score", "reaches_c", "n_route_steps", "cost_low", "cost_high", "cost_mid",
    "within_cap", "within_cap_worst_case", "cert_age_years", "is_stale", "confidence",
    "likely_exempt_cost_cap", "lock_in_candidate", "needs_survey", "notes",
    "cap_cost_low", "cap_cost_high", "cap_cost_mid", "cost_known", "score_exact", "borderline",
    "cap_verdict_uncertain", "exempt_reason",
    "alt_cost_low", "alt_cost_high", "alt_cost_mid", "alt_cap_cost_mid", "alt_final_score",
    "alt_within_cap", "safer_cost_low", "safer_cost_high", "safer_cost_mid",
    "safer_cap_cost_mid", "safer_final_score", "safer_within_cap", "lock_in_needs_boiler",
]  # fmt: skip
STEP_COLS = [
    "run_id", "property_id", "certificate_number", "step_type", "step_order", "sequence",
    "improvement_type", "uplift", "cost_low", "cost_high", "cost_mid", "cumulative_score",
    "cumulative_band", "cumulative_cost_low", "cumulative_cost_high", "cumulative_cost_mid",
    "counts_toward_cap",
]  # fmt: skip
PRED_COLS = [
    "run_id", "engine_version", "as_of", "created_at", "property_id", "uprn",
    "certificate_number", "current_score", "predicted_final_score", "reaches_c",
    "cost_low", "cost_high", "cost_mid", "confidence", "route",
]  # fmt: skip


def load_inputs(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int | None, Certificate]]:
    steps_by_cert: dict[str, list[Step]] = defaultdict(list)
    for num, seq, cat, typ, cost_text, rating in con.execute(
        """SELECT certificate_number, sequence, improvement_category, improvement_type,
                  indicative_cost_text, rating_after
           FROM marts.fct_recommendations_for_engine"""
    ).fetchall():
        steps_by_cert[num].append(Step(seq or 0, rating, cost_text, cat, typ))

    out = []
    for pid, uprn, num, cur, pot, reg in con.execute(
        """SELECT property_id, uprn, certificate_number, current_score, potential_score,
                  registration_date
           FROM marts.fct_property_compliance"""
    ).fetchall():
        cert = Certificate(num, cur, reg, pot, tuple(steps_by_cert.get(num, ())), uprn)
        out.append((pid, uprn, cert))
    return out


def _summary_cols(x: RouteSummary | None) -> tuple:
    if x is None:
        return (None,) * 6
    return (x.cost_low, x.cost_high, x.cost_mid, x.cap_cost_mid, x.final_score, x.within_cap)


def result_rows(run_id, created_at, as_of, pid, uprn, r: RouteResult):
    result = (
        run_id, __version__, as_of, created_at, pid, uprn, r.certificate_number,
        r.current_score, r.current_band, r.below_c, r.gap_to_c, r.final_score, r.reaches_c,
        len(r.steps), r.cost_low, r.cost_high, r.cost_mid, r.within_cap,
        r.within_cap_worst_case, r.cert_age_years, r.is_stale, r.confidence,
        r.likely_exempt_cost_cap, r.lock_in_candidate, r.needs_survey, " | ".join(r.notes),
        r.cap_cost_low, r.cap_cost_high, r.cap_cost_mid, r.cost_known, r.score_exact,
        r.borderline, r.cap_verdict_uncertain, r.exempt_reason,
        *_summary_cols(r.alternative), *_summary_cols(r.safer), r.lock_in_needs_boiler,
    )  # fmt: skip
    steps = [
        (
            run_id,
            pid,
            r.certificate_number,
            step_type,
            i,
            s.sequence,
            s.improvement_type,
            s.uplift,
            s.cost_low,
            s.cost_high,
            s.cost_mid,
            s.cumulative_score,
            s.cumulative_band,
            s.cumulative_cost_low,
            s.cumulative_cost_high,
            s.cumulative_cost_mid,
            s.counts_toward_cap,
        )  # fmt: skip
        for step_type, group in (
            ("route", r.steps),
            ("further_measure", r.further_measures),
            ("alternative", r.alternative.steps if r.alternative else ()),
            ("safer", r.safer.steps if r.safer else ()),
        )
        for i, s in enumerate(group, start=1)
    ]
    pred = (
        run_id, __version__, as_of, created_at, pid, uprn, r.certificate_number,
        r.current_score, r.final_score, r.reaches_c, r.cost_low, r.cost_high, r.cost_mid,
        r.confidence, json.dumps([asdict(s) for s in r.steps]),
    )  # fmt: skip
    return result, steps, pred


_NULL = "\\N"  # CSV marker for NULL, so empty strings stay empty strings


def _write(con, table: str, cols: list[str], rows: list[tuple]) -> None:
    """Stage rows in a separate local in-memory DuckDB, export to Parquet, then insert in one
    statement. Against MotherDuck, per-row inserts (even into TEMP tables) cost a network
    round trip each; this uploads one file per table instead."""
    if not rows:
        return
    col_list = ", ".join(cols)
    types = con.execute(f"SELECT {col_list} FROM {table} LIMIT 0").description
    ddl_types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            table.split("."),
        ).fetchall()
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path, path = Path(tmpdir) / "rows.csv", Path(tmpdir) / "rows.parquet"
        # Bulk-load via CSV: executemany into DuckDB is row-by-row and took ~8 min per run.
        with csv_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([_NULL if v is None else v for v in row] for row in rows)
        columns = "{" + ", ".join(f"'{d[0]}': '{ddl_types[d[0]]}'" for d in types) + "}"
        local = duckdb.connect(":memory:")
        local.execute(
            f"COPY (SELECT * FROM read_csv(?, header = true, nullstr = '{_NULL}', "
            f"quote = '\"', escape = '\"', columns = {columns})) TO '{path}' (FORMAT parquet)",
            [str(csv_path)],
        )
        local.close()
        con.execute(
            f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM read_parquet(?)", [str(path)]
        )


def run(con: duckdb.DuckDBPyConnection, policy: Policy, as_of: date) -> dict[str, int]:
    run_id = uuid.uuid4().hex[:12]
    created_at = datetime.now(UTC).replace(tzinfo=None)
    results, steps, preds = [], [], []
    for pid, uprn, cert in load_inputs(con):
        res, st, pred = result_rows(
            run_id, created_at, as_of, pid, uprn, route_to_c(cert, policy, as_of)
        )
        results.append(res)
        steps.extend(st)
        preds.append(pred)

    con.execute("BEGIN")  # drop + recreate + write in one transaction: a failed run keeps old data
    try:
        con.execute(DDL)
        _write(con, "analytics.route_results", RESULT_COLS, results)
        _write(con, "analytics.route_steps", STEP_COLS, steps)
        _write(con, "analytics.predictions", PRED_COLS, preds)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    summary = {"run_id": run_id, "properties": len(results), "steps": len(steps)}
    log.info("engine run %s (v%s): %s", run_id, __version__, summary)
    return summary


def main(argv: list[str] | None = None) -> None:
    import os

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="engine.run")
    p.add_argument("--target", default=os.environ.get("DUCKDB_TARGET", "md:route_to_c"))
    p.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = p.parse_args(argv)
    con = duckdb.connect(args.target)
    run(con, load_policy(), args.as_of)
    con.close()


if __name__ == "__main__":
    main()
