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
