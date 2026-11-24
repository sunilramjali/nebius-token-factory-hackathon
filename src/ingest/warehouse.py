"""Warehouse connection + raw table DDL.

Target is set by DUCKDB_TARGET:
  - a file path (default `data/epc.duckdb`) for local dev
  - `md:<database>` for MotherDuck (needs MOTHERDUCK_TOKEN in the environment)
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

DEFAULT_TARGET = "data/epc.duckdb"

RAW_DDL = """
CREATE SCHEMA IF NOT EXISTS raw;

-- One row per certificate returned by /api/domestic/search (summary fields only).
CREATE TABLE IF NOT EXISTS raw.epc_certificate_index (
    certificate_number VARCHAR PRIMARY KEY,
    uprn               BIGINT,
    current_band       VARCHAR,
    registration_date  DATE,
    schema_type        VARCHAR,
    council            VARCHAR,
    constituency       VARCHAR,
    address_line1      VARCHAR,  -- address fields: OS/Royal Mail terms, display-only (ADR-007)
    address_line2      VARCHAR,
    address_line3      VARCHAR,
    address_line4      VARCHAR,
    post_town          VARCHAR,
    postcode           VARCHAR,
    indexed_at         TIMESTAMP DEFAULT current_timestamp
);

-- Full certificate document from /api/certificate, stored as raw JSON (ELT; dbt parses it).
CREATE TABLE IF NOT EXISTS raw.epc_certificate_documents (
    certificate_number VARCHAR PRIMARY KEY,
    found              BOOLEAN,
    document           JSON,
    fetched_at         TIMESTAMP DEFAULT current_timestamp
);
"""


def connect(target: str | None = None) -> duckdb.DuckDBPyConnection:
    target = target or os.environ.get("DUCKDB_TARGET") or DEFAULT_TARGET
    if target.startswith("md:"):
        db = target.removeprefix("md:")
        con = duckdb.connect("md:")
        if db:
            con.execute(f'CREATE DATABASE IF NOT EXISTS "{db}"')
            con.execute(f'USE "{db}"')
    else:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(target)
    con.execute(RAW_DDL)
    return con
