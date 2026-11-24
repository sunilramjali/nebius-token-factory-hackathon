# CLAUDE.md — Nebius Token Factory Hackathon (EPC Route-to-C)

Hackathon fork of `../route-to-c` (ADR-001 in `docs/decisions.md`). Same rules, smaller scope.

## Goal
Demo: landlord enters a few Newham addresses → table of band, gap to C, cheapest route,
cap and lock-in flags → chat with an assistant (Nebius Token Factory, OpenAI-compatible API)
that explains using only tool outputs.

## Rules
- **Code does the maths, the LLM explains.** The LLM never invents ratings, costs or legal
  conclusions. Every answer is labelled "indicative — a survey by an accredited assessor is required".
- Policy values come from `config/policy.yaml`; never hard-code them. It's proposed policy, not law.
- Landlord-facing wording follows `docs/report_style.md` (skill: `report-wording`).
- Every bug fix gets a regression test. Run `uv run pytest` after changes.
- Key on UPRN. Address fields are display-only for properties the user entered; `data/` is
  gitignored and never shared (`docs/data_access.md` §3).
- Secrets in `.env` only (`NEBIUS_API_KEY`, `NEBIUS_MODEL`). Ask before adding dependencies.
- Not building: fine-tuning, area maps, auth, payments.
