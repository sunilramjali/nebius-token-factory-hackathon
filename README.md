# Nebius Token Factory Hackathon: EPC Route-to-C

An AI assistant that tells small-portfolio landlords in Newham (East London) which of their
rental homes are below EPC C, the cheapest route to C within the £10k cost cap, and which
ones to lock in before **30 September 2029**.

**Code does the maths; the LLM explains.** A deterministic engine computes every rating,
cost and flag from the public EPC register. An open model on
[Nebius Token Factory](https://tokenfactory.nebius.com) answers questions using only tool
outputs. All results are **indicative — a survey by an accredited assessor is required.**

> Policy note: EPC C for private rentals by 1 Oct 2030 is **proposed, not yet law**
> (GOV.UK government response, 21 Jan 2026). Values live in `config/policy.yaml`.

## Origin

Standalone copy of the core of the `route-to-c` project (engine, address lookup, report,
policy config), made for the hackathon. See `docs/decisions.md` (ADR-001).

## Setup

```bash
uv sync
cp .env.example .env            # add NEBIUS_API_KEY and NEBIUS_MODEL
uv run --env-file .env python scripts/snapshot.py   # one-off copy of the data from MotherDuck
uv run pytest
```

## Layout

| Path | What |
|---|---|
| `src/engine/` | Route-to-C engine: cheapest set of measures reaching SAP 69, cap and lock-in flags |
| `src/lookup/` | Address → UPRN matching (strict; wrong match is worse than none) |
| `src/report/` | HTML portfolio report |
| `src/agent/` | *To build:* Nebius tool-calling assistant |
| `app.py` | *To build:* Streamlit demo |
| `config/policy.yaml` | MEES dates, cost cap, penalties with sources |
| `scripts/snapshot.py` | Copies the needed tables to `data/epc.duckdb` |

## Data and licence

EPC register data (MHCLG). Non-address fields and UPRNs: Open Government Licence v3.0.
Address and postcode fields are restricted to energy-efficiency purposes, so the local
database is gitignored and addresses are only shown for properties the user enters.
See `docs/data_access.md` §3.
