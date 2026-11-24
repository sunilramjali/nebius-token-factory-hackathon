import json
from datetime import date

import duckdb
import pytest

from engine.policy import Policy
from engine.run import run

POLICY = Policy(69, 10_000, date(2029, 9, 30), 10)


def make_marts():
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA marts")
    con.execute("""CREATE TABLE marts.fct_property_compliance AS SELECT * FROM (VALUES
        ('100', 100::BIGINT, 'C1', 41, 78, DATE '2025-10-31'),
        ('200', 200::BIGINT, 'C2', 75, 80, DATE '2022-01-01'),
        ('cert:C3', NULL::BIGINT, 'C3', 50, 50, DATE '2012-01-01')
    ) t(property_id, uprn, certificate_number, current_score, potential_score,
        registration_date)""")
    con.execute("""CREATE TABLE marts.fct_recommendations_for_engine AS SELECT * FROM (VALUES
        ('C1', 1, '5', 'A2', '£900 - £1,200', 68),
        ('C1', 2, '5', 'L2', '£800 - £1,600', 78),
        ('C1', 3, '3', 'U', '£3,500 - £5,500', 85),
        ('C2', 1, '5', 'E', '£100', 77)
    ) t(certificate_number, sequence, improvement_category, improvement_type,
        indicative_cost_text, rating_after)""")
    return con


def test_run_writes_results_steps_and_appends_predictions():
    con = make_marts()
    s1 = run(con, POLICY, date(2026, 11, 6))
    assert s1["properties"] == 3

    rows = dict(
        con.execute("SELECT property_id, lock_in_candidate FROM analytics.route_results").fetchall()
    )
    assert rows == {"100": True, "200": False, "cert:C3": False}

    r = con.execute("""SELECT final_score, cost_mid, n_route_steps, confidence
                       FROM analytics.route_results WHERE property_id = '100'""").fetchone()
    assert r == (78, 2250.0, 2, "high")

    steps = con.execute("""SELECT step_type, improvement_type FROM analytics.route_steps
                           WHERE property_id = '100' ORDER BY step_type, step_order""").fetchall()
    assert steps == [("further_measure", "U"), ("route", "A2"), ("route", "L2")]

    no_recs = con.execute("""SELECT needs_survey, likely_exempt_cost_cap FROM
        analytics.route_results WHERE property_id = 'cert:C3'""").fetchone()
    assert no_recs == (True, False)

    route = json.loads(
        con.execute("SELECT route FROM analytics.predictions WHERE property_id = '100'").fetchone()[
            0
        ]
    )
    assert [s["improvement_type"] for s in route] == ["A2", "L2"]

    # second run replaces latest results but appends predictions
    s2 = run(con, POLICY, date(2026, 11, 7))
    assert s2["run_id"] != s1["run_id"]
    assert con.execute("SELECT count(*) FROM analytics.route_results").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM analytics.predictions").fetchone()[0] == 6
    assert (
        con.execute("SELECT count(DISTINCT run_id) FROM analytics.route_steps").fetchone()[0] == 1
    )


def test_audit_passes_on_a_clean_run_and_catches_corruption():
    from engine.audit import audit

    con = make_marts()
    run(con, POLICY, date(2026, 11, 6))
    assert audit(con, POLICY, target=":memory:") == []

    # A lock-in with an unknown cost must fail the audit, and so must a stored result that no
    # longer matches what the engine computes.
    con.execute("UPDATE analytics.route_results SET cost_known = false WHERE property_id = '100'")
    con.execute("UPDATE analytics.route_results SET cost_mid = 1 WHERE property_id = '200'")
    failures = audit(con, POLICY, target=":memory:")
    assert any(f.startswith("lock-in implies") for f in failures)
    assert any(f.startswith("stored vs recomputed") for f in failures)


def test_recreating_tables_keeps_old_results_if_the_run_fails(monkeypatch):
    import engine.run as engine_run

    con = make_marts()
    run(con, POLICY, date(2026, 11, 6))

    def boom(*a, **k):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(engine_run, "_write", boom)
    with pytest.raises(RuntimeError):
        run(con, POLICY, date(2026, 11, 7))
    assert con.execute("SELECT count(*) FROM analytics.route_results").fetchone()[0] == 3


def test_bulk_write_round_trips_nulls_quotes_json_and_types():
    from datetime import datetime

    from engine.run import _write

    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA s")
    con.execute("""CREATE TABLE s.t (a VARCHAR, b INTEGER, c DOUBLE, d BOOLEAN, e DATE,
                   f TIMESTAMP, g JSON)""")
    rows = [
        (
            "",
            1,
            1.5,
            True,
            date(2026, 11, 6),
            datetime(2026, 11, 6, 12, 0),
            '[{"x": "a, \\"b\\""}]',
        ),
        (None, None, None, None, None, None, None),
        ('say "hi", then\nleave', -3, 0.0, False, date(2029, 9, 30), datetime(2026, 1, 1), "[]"),
    ]
    _write(con, "s.t", list("abcdefg"), rows)
    got = con.execute(
        "SELECT a, b, c, d, e, f, g::VARCHAR FROM s.t ORDER BY b NULLS LAST"
    ).fetchall()
    assert got[0] == rows[2][:6] + ("[]",)
    assert got[1][:6] == rows[0][:6] and json.loads(got[1][6]) == [{"x": 'a, "b"'}]
    assert got[2] == (None,) * 7  # NULL stays NULL, '' stays ''
