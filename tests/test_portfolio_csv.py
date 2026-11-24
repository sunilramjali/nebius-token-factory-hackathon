"""Landlord CSV → UPRNs → report. Synthetic addresses only (ADR-007)."""

from datetime import date

import duckdb
import pytest

from engine.policy import Policy
from lookup.portfolio import read_portfolio_csv
from report.portfolio_report import render, report_from_csv

POLICY = Policy(69, 10_000, date(2029, 9, 30), 10)


def warehouse():
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA raw; CREATE SCHEMA analytics; CREATE SCHEMA marts")
    con.execute("""CREATE TABLE raw.epc_certificate_index AS SELECT * FROM (VALUES
        (1::BIGINT, '12 Example Road', NULL, 'E99 9ZZ', DATE '2023-01-01'),
        (2::BIGINT, 'Flat A', '40 Example Road', 'E99 9ZZ', DATE '2023-01-01'),
        (3::BIGINT, 'Flat B', '40 Example Road', 'E99 9ZZ', DATE '2023-01-01'),
        (4::BIGINT, '5 Sample Street', NULL, 'E99 9ZZ', DATE '2015-01-01')
    ) t(uprn, address_line1, address_line2, postcode, registration_date)""")
    con.execute("""ALTER TABLE raw.epc_certificate_index ADD COLUMN address_line3 VARCHAR;
                   ALTER TABLE raw.epc_certificate_index ADD COLUMN address_line4 VARCHAR""")
    con.execute("""CREATE TABLE marts.fct_property_compliance AS SELECT * FROM (VALUES
        ('1', 'E99 9ZZ', 'house', DATE '2023-01-01'),
        ('2', 'E99 9ZZ', 'flat', DATE '2023-01-01')
    ) t(property_id, postcode, property_type, registration_date)""")
    con.execute("""CREATE TABLE analytics.route_results AS SELECT * FROM (VALUES
        ('1', 60, 'D', 72, true, 400.0, 900.0, 650.0, true, 'high', true, false, false, '',
         400.0, 900.0, true, false, true, false, NULL, false),
        ('2', 75, 'C', 75, true, 0.0, 0.0, 0.0, true, 'high', false, false, false, '',
         0.0, 0.0, true, false, true, false, NULL, false)
    ) t(property_id, current_score, current_band, final_score, reaches_c, cost_low,
        cost_high, cost_mid, within_cap, confidence, lock_in_candidate,
        likely_exempt_cost_cap, needs_survey, notes, cap_cost_low, cap_cost_high, cost_known,
        borderline, score_exact, cap_verdict_uncertain, exempt_reason, lock_in_needs_boiler)""")
    con.execute("""CREATE TABLE analytics.route_steps (property_id VARCHAR,
        certificate_number VARCHAR, sequence INT, step_type VARCHAR, step_order INT,
        improvement_type VARCHAR, uplift INT, cost_low DOUBLE, cost_high DOUBLE,
        cumulative_score INT, cumulative_band VARCHAR, counts_toward_cap BOOLEAN)""")
    con.execute("""INSERT INTO analytics.route_steps VALUES
        ('1', 'C1', 1, 'route', 1, 'A', 12, 400, 900, 72, 'C', true)""")
    con.execute("""CREATE TABLE marts.fct_recommendations_for_engine AS
        SELECT 'C1' AS certificate_number, 1 AS sequence,
               'Loft insulation' AS improvement_summary""")
    return con


def write_csv(tmp_path, text):
    p = tmp_path / "portfolio.csv"
    p.write_text(text)
    return p


def test_csv_to_report_with_needs_input(tmp_path):
    csv = write_csv(tmp_path, (
        "Address,Postcode,Label\n"
        "12 example rd,e99 9zz,Home one\n"
        "40 Example Road,E99 9ZZ,Which flat?\n"
        "Flat A 40 Example Road E99 9ZZ,,\n"
        "99 Example Road,E99 9ZZ,Wrong number\n"
        "12 Example Road,E99 9ZZ,Again\n"
        "5 Sample Street,E99 9ZZ,No cert details\n"
        "1 Nowhere Lane,E11 1AA,Other area\n"
        ",,\n"
    ))  # fmt: skip
    props, needs = report_from_csv(warehouse(), csv)

    assert [(p.property_id, p.label) for p in props] == [
        ("1", "Home one"),
        ("2", "Flat A 40 Example Road E99 9ZZ"),  # label defaults to the typed address
    ]
    assert props[0].register_address == "12 Example Road, E99 9ZZ"
    assert [(r.row.line, r.status) for r in needs] == [
        (3, "ambiguous"), (5, "no_match"), (6, "duplicate"), (7, "no_data"),
        (8, "no_candidates"),
    ]  # fmt: skip
    assert needs[0].options == [(2, "Flat A, 40 Example Road"), (3, "Flat B, 40 Example Road")]

    out = render(props, POLICY, "Your portfolio", date(2026, 11, 6), needs)
    assert "Home one" in out and "Register record: 12 Example Road, E99 9ZZ" in out
    assert "Needs your input (5)" in out
    assert "<code>3</code> — Flat B, 40 Example Road" in out
    assert "1 of 2</b>below EPC C" in out  # needs-input rows aren't counted


def test_uprn_column_settles_ambiguous_rows(tmp_path):
    csv = write_csv(tmp_path, "address,uprn\n40 Example Road E99 9ZZ,2\n")
    props, needs = report_from_csv(warehouse(), csv)
    assert [p.property_id for p in props] == ["2"] and needs == []


def test_csv_validation(tmp_path):
    with pytest.raises(ValueError, match="'address' or 'uprn'"):
        read_portfolio_csv(write_csv(tmp_path, "street,town\nx,y\n"))
    with pytest.raises(ValueError, match="line 2: uprn must be a number"):
        read_portfolio_csv(write_csv(tmp_path, "address,uprn\nx,abc\n"))


def test_big_block_asks_for_flat_number_instead_of_listing():
    from lookup.portfolio import PortfolioRow, Resolved
    from report.portfolio_report import render_needs_input

    row = PortfolioRow(2, "1 Block Road", "E99 9ZZ", "Block", None)
    r = Resolved(row, "ambiguous", options=[(i, f"Flat {i}, 1 Block Road") for i in range(12)])
    out = render_needs_input([r])
    assert "12 homes share this address" in out and "Flat 3," not in out
