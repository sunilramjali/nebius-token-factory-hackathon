"""Post-run data audit: invariants every engine run must satisfy (ADR-011).

Runs after `engine.run` in CI (synthetic fixture) and in the Transform workflow (MotherDuck).
Exits non-zero if any check fails, so a bad run never goes unnoticed. Aggregates only: it
prints counts, never addresses.

Usage: python -m engine.audit [--target md:route_to_c] [--sample 500]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import duckdb
from dotenv import load_dotenv

from engine.policy import Policy, load_policy
from engine.route_to_c import route_to_c
from engine.run import load_inputs

log = logging.getLogger("engine.audit")


def sql_checks(policy: Policy) -> list[tuple[str, str]]:
    """(name, SQL returning the number of violating rows)."""
    cap = policy.cost_cap_gbp
    c = policy.band_c_threshold
    fossil = ", ".join(f"'{t}'" for t in sorted(policy.fossil_heating_types)) or "NULL"
    rr = "analytics.route_results"
    return [
        ("one result per property",
         f"SELECT abs((SELECT count(*) FROM {rr}) - "
         "(SELECT count(*) FROM marts.fct_property_compliance))"),
        ("one engine run in route_results", f"SELECT count(DISTINCT run_id) - 1 FROM {rr}"),
        ("lock-in implies below C, reaches C, known cost, a route within the cap",
         f"SELECT count(*) FROM {rr} WHERE lock_in_candidate AND NOT (below_c AND reaches_c "
         f"AND cost_known AND (cap_cost_mid <= {cap} OR lock_in_needs_boiler AND alt_within_cap))"),
        ("lock-in via boiler only when the main route is over the cap and the boiler one fits",
         f"SELECT count(*) FROM {rr} WHERE lock_in_needs_boiler AND NOT (lock_in_candidate "
         f"AND cap_cost_mid > {cap} AND alt_within_cap)"),
        ("lock-in and exemption never both set",
         f"SELECT count(*) FROM {rr} WHERE lock_in_candidate AND likely_exempt_cost_cap"),
        ("exemption flag matches exempt_reason",
         f"SELECT count(*) FROM {rr} WHERE likely_exempt_cost_cap <> (exempt_reason IS NOT NULL)"),
        ("over_cap means a known cap cost above the cap and no boiler route within it",
         f"SELECT count(*) FROM {rr} WHERE exempt_reason = 'over_cap' AND NOT (reaches_c "
         f"AND cost_known AND cap_cost_mid > {cap} AND NOT coalesce(alt_within_cap, false))"),
        ("cannot_reach_c means C not reached",
         f"SELECT count(*) FROM {rr} WHERE exempt_reason = 'cannot_reach_c' AND reaches_c"),
        ("unknown cost means low confidence and no cap verdict",
         f"SELECT count(*) FROM {rr} WHERE NOT cost_known AND (confidence <> 'low' "
         "OR within_cap AND below_c OR lock_in_candidate OR exempt_reason = 'over_cap')"),
        ("costs ordered: low <= mid <= high, cap cost <= total",
         f"SELECT count(*) FROM {rr} WHERE NOT (cost_low <= cost_mid AND cost_mid <= cost_high "
         "AND cap_cost_mid <= cost_mid + 0.001 AND cap_cost_low <= cap_cost_high)"),
        ("borderline only for estimated scores just at C",
         f"SELECT count(*) FROM {rr} WHERE borderline AND (score_exact OR final_score < {c} "
         f"OR final_score >= {c} + 2)"),
        ("reaches_c matches final score",
         f"SELECT count(*) FROM {rr} WHERE reaches_c <> (final_score >= {c})"),
        ("at or above C has no route and no flags",
         f"SELECT count(*) FROM {rr} WHERE NOT below_c AND (n_route_steps > 0 "
         "OR lock_in_candidate OR likely_exempt_cost_cap)"),
        ("final score = current + route uplifts",
         f"SELECT count(*) FROM {rr} r LEFT JOIN (SELECT property_id, sum(uplift) up, count(*) n "
         "FROM analytics.route_steps WHERE step_type = 'route' GROUP BY 1) s USING (property_id) "
         "WHERE r.final_score <> r.current_score + coalesce(s.up, 0) "
         "OR r.n_route_steps <> coalesce(s.n, 0)"),
        ("route steps have positive uplift",
         "SELECT count(*) FROM analytics.route_steps WHERE step_type = 'route' AND uplift <= 0"),
        ("fossil heating never counts toward the cap",
         "SELECT count(*) FROM analytics.route_steps WHERE step_type = 'route' AND "
         f"improvement_type IN ({fossil}) AND counts_toward_cap"),
        ("cap cost = sum of counted route steps",
         f"SELECT count(*) FROM {rr} r LEFT JOIN (SELECT property_id, "
         "sum(cost_mid) FILTER (WHERE counts_toward_cap) cm FROM analytics.route_steps "
         "WHERE step_type = 'route' GROUP BY 1) s USING (property_id) "
         "WHERE abs(r.cap_cost_mid - coalesce(s.cm, 0)) > 0.01"),
        ("alternative is cheaper, or the only route within the cap",
         f"SELECT count(*) FROM {rr} WHERE alt_cost_mid IS NOT NULL AND NOT "
         "(alt_cost_mid < cost_mid OR alt_within_cap AND NOT within_cap)"),
        ("alternative uses fossil heating; main route doesn't when there's an alternative",
         f"SELECT count(*) FROM {rr} r WHERE alt_cost_mid IS NOT NULL AND (NOT EXISTS ("
         "SELECT 1 FROM analytics.route_steps s WHERE s.property_id = r.property_id AND "
         "s.step_type = 'alternative' AND NOT s.counts_toward_cap) OR EXISTS ("
         "SELECT 1 FROM analytics.route_steps s WHERE s.property_id = r.property_id AND "
         "s.step_type = 'route' AND NOT s.counts_toward_cap))"),
        ("safer route only for borderline, reaches C + 2, costs at least the main route",
         f"SELECT count(*) FROM {rr} WHERE safer_cost_mid IS NOT NULL AND NOT (borderline "
         f"AND safer_final_score >= {c} + 2 AND safer_cost_mid >= cost_mid - 0.001)"),
        ("secondary route cap costs = sum of counted steps",
         f"SELECT count(*) FROM {rr} r LEFT JOIN (SELECT property_id, step_type, "
         "sum(cost_mid) FILTER (WHERE counts_toward_cap) cm FROM analytics.route_steps "
         "WHERE step_type IN ('alternative', 'safer') GROUP BY 1, 2) s USING (property_id) "
         "WHERE s.step_type = 'alternative' AND abs(r.alt_cap_cost_mid - coalesce(s.cm, 0)) > 0.01 "
         "OR s.step_type = 'safer' AND abs(r.safer_cap_cost_mid - coalesce(s.cm, 0)) > 0.01"),
    ]  # fmt: skip


def recompute_sample(con, policy: Policy, sample: int) -> int:
    """Stored results must equal a fresh engine call (catches stale tables or code drift)."""
    stored = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT property_id, as_of, final_score, cost_mid, cap_cost_mid, lock_in_candidate, "
            "exempt_reason, alt_cost_mid, safer_cost_mid FROM analytics.route_results"
        ).fetchall()
    }
    inputs = load_inputs(con)
    step = max(len(inputs) // sample, 1)
    bad = 0
    for pid, _, cert in inputs[::step]:
        s = stored.get(pid)
        if s is None:
            bad += 1
            continue
        r = route_to_c(cert, policy, s[0])
        fresh = (
            r.final_score, r.cost_mid, r.cap_cost_mid, r.lock_in_candidate, r.exempt_reason,
            r.alternative.cost_mid if r.alternative else None,
            r.safer.cost_mid if r.safer else None,
        )  # fmt: skip
        if fresh != tuple(s[1:]):
            bad += 1
            log.error("property %s: stored %s, recomputed %s", pid, s[1:], fresh)
    return bad


def audit(con, policy: Policy, target: str, sample: int = 500) -> list[str]:
    failures = []
    for name, sql in sql_checks(policy):
        n = con.execute(sql).fetchone()[0] or 0
        log.info("%-70s %s", name, "ok" if n == 0 else f"FAIL ({n})")
        if n:
            failures.append(f"{name}: {n}")
    if target.startswith("md:"):  # production must never contain the synthetic CI fixture
        n = con.execute(
            "SELECT count(*) FROM raw.epc_certificate_documents WHERE certificate_number "
            "NOT SIMILAR TO '[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{4}'"
        ).fetchone()[0]
        log.info("%-70s %s", "no synthetic certificates in production", "ok" if not n else n)
        if n:
            failures.append(f"non-register certificate numbers in production: {n}")
    n = recompute_sample(con, policy, sample)
    log.info("%-70s %s", f"stored results match a fresh engine run (sample {sample})",
             "ok" if not n else f"FAIL ({n})")  # fmt: skip
    if n:
        failures.append(f"stored vs recomputed mismatches: {n}")
    return failures


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="engine.audit")
    p.add_argument("--target", default=os.environ.get("DUCKDB_TARGET", "md:route_to_c"))
    p.add_argument("--sample", type=int, default=500)
    args = p.parse_args(argv)
    con = duckdb.connect(args.target, read_only=True)
    failures = audit(con, load_policy(), args.target, args.sample)
    con.close()
    if failures:
        log.error("audit FAILED: %s", "; ".join(failures))
        sys.exit(1)
    log.info("audit passed")


if __name__ == "__main__":
    main()
