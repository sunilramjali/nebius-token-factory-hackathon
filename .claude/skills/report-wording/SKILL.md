---
name: report-wording
description: Write or review landlord-facing wording (report templates, app text, agent replies) against docs/report_style.md. Use when changing any sentence a landlord will read, adding a status or flag to the report, or when asked to improve the report's wording.
---

# Report wording (route-to-C)

1. Read `docs/report_style.md` first. It covers the reader, plain-English rules, banned words, legal hedges, status labels and layout.
2. Make the change in `src/report/portfolio_report.py`.
   - Policy values come from `Policy` (`config/policy.yaml`), never literals.
   - A new status needs an entry in `status()`, `ACTIONS` and the style guide's table.
3. Run `uv run pytest -q tests/test_report.py tests/test_report_wording.py`. The wording tests render one home of every status and check:
   - sentence length (≤ 25 words);
   - banned words;
   - legal hedges;
   - that no policy value is hard-coded (by rendering with made-up policy values).
4. A new template sentence must be reachable from `every_status()` in `tests/test_report_wording.py`. Add a home for it if needed.
5. Read the regenerated sample as a landlord would: `uv run python -m report.portfolio_report --pick-samples`, then open `reports/sample_portfolio.html`. Ask:
   - Can a non-technical reader say what to do next within 5 seconds?
   - Does any sentence sound like a legal conclusion? Rewrite it as "likely", "may" or "would".
   - Is every number traceable? Run the `verify-results` skill if numbers changed.
6. Readability is a check, not a target: paste the visible text into a readability checker (e.g. Hemingway) if a section feels dense.
