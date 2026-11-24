# Decisions (ADR log)

## ADR-001 — Separate hackathon repo, copied core, Nebius as provider
- **Date:** 2026-11-24
- **Status:** accepted
- **Context:** Entering the Nebius Token Factory Hackathon. The main `route-to-c` project gates
  its AI assistant (Phase 2) behind landlord validation (Phase 0.5), which isn't finished.
- **Decision:**
  - New standalone repo with a *copy* of `src/engine`, `src/lookup`, `src/report`,
    `ingest/warehouse.py` (connection only), `config/policy.yaml` and their tests. No dbt and no
    ingest: data comes from a one-off snapshot of the built MotherDuck tables (`scripts/snapshot.py`).
  - LLM provider: Nebius Token Factory via the `openai` SDK. Model chosen from the live catalog
    (`NEBIUS_MODEL`), must support tool calling.
  - Local only until the owner decides to publish. Data stays on the owner's laptop (`data/epc.duckdb`, gitignored); judges see a live demo.
- **Consequences:** Runs without the dbt pipeline, from a local snapshot that is never committed or shared (address licence); the owner demos live from their laptop. The two
  copies can drift: engine fixes must be ported by hand in either direction. Skipping the
  validation gate applies to this repo only; the main project's gate is unchanged.

## ADR-002 — Publish the code on GitHub (public)
- **Date:** 2026-11-24
- **Status:** accepted
- **Context:** ADR-001 kept the repo local until the owner decided to publish. The owner has
  decided to publish it for the hackathon.
- **Decision:** Public GitHub repo `nebius-token-factory-hackathon`. Code, tests, docs and
  config only. Before publishing, the full git history was scanned: no secrets, and every
  address, postcode and UPRN in it is a placeholder.
- **Consequences:** `data/` (snapshot, eval output, portfolios) and `.env` stay gitignored and
  local; the demo still runs from the owner's laptop. Anyone cloning needs their own snapshot
  access and Nebius key. Re-scan before each push if tests or examples gain new fixtures:
  register addresses must never be committed (`docs/data_access.md` §3).

