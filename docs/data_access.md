# Data access — England & Wales domestic EPC register

> Researched 2026-11-06. Sources linked inline. The service is labelled **Beta**, so re-check before relying on details.
> This is a reading of public guidance, not legal advice.

## 1. Access route

The official service is **Get Energy Performance of Buildings Data**, run by the MHCLG EPC register team:
<https://get-energy-performance-data.communities.gov.uk/>

- The old `epc.opendatacommunities.org` docs URLs now 301-redirect to the new service. Do not build against the old API.
- Coverage: England and Wales, domestic certificates lodged **since 2008** (including expired or replaced ones). Scotland is a separate register and out of scope.

There are two routes:

| Route | What | Use for |
|---|---|---|
| **Bulk download** | Full-load ZIP (CSV or JSON) for all domestic certificates, served via a pre-signed S3 URL (`/api/files/domestic/csv`, `/api/files/domestic/csv/info` for metadata) | Loading all Newham certificates into DuckDB for the portfolio-wide marts |
| **Developer API** | Base URL `https://api.get-energy-performance-data.communities.gov.uk`. OpenAPI spec: [communitiesuk/epb-data-warehouse `api/api.yml`](https://github.com/communitiesuk/epb-data-warehouse/blob/main/api/api.yml) | Looking up a landlord's properties and fetching full certificate documents |

Key API endpoints (from the OpenAPI spec):
- `GET /api/domestic/search`: filters `council[]` (local authority name, e.g. `Newham`), `constituency[]`, `postcode`, `uprn`, `address` (partial), `efficiency_rating[]`, `date_start`/`date_end`. Pagination is `current_page` + `page_size` (**max 5000**).
- `GET /api/certificate?certificate_number=…`: the **full EPC document as JSON** (free-form schema).
- `GET /api/deltas?date_start&date_end`: certificates changed between two dates. Use this for incremental refresh and the later calibration job.
- `GET /api/codes`, `/api/codes/info`: code lookups.

## 2. Authentication

- Both bulk download and API need a **GOV.UK One Login** account.
- After signing in, the service issues a **bearer token** for the API ([API guidance](https://get-energy-performance-data.communities.gov.uk/guidance/energy-certificate-data-apis)). The page doesn't say how long a token lasts.
- There is no API-key or basic-auth scheme.
- **For this project:** the user signs in manually and puts the token in `.env` as `EPC_API_TOKEN`. Never commit it.

## 3. Licence

Source: [Licensing restrictions](https://get-energy-performance-data.communities.gov.uk/guidance/licensing-restrictions)

- **Most fields are under the Open Government Licence v3.0.** Quote: "All data fields other than the address and postcode data (address, address 1, address 2, address 3, postcode) … are licensed under the Open Government Licence v3.0, which includes Ordnance Survey UPRNs."
  - **UPRN is under the OGL.** Ratings, scores, fabric descriptions and recommendations are under the OGL.
- **Address and postcode fields are restricted.** They are derived from OS AddressBase Premium and Royal Mail PAF. Use is permitted only for listed energy-performance purposes, which include:
  - effective management of properties with a view to promoting energy efficiency
  - promoting energy efficiency in the sale or rental market
  - research and statistics on the energy efficiency of buildings
  - promoting and marketing improvements made through government programmes
  - enforcement, Building Act duties, and crime and courts
  - Other uses need a licence from Ordnance Survey (PricingLicensing@os.uk) and/or Royal Mail (address.management@royalmail.com).
- **If you share address data**, you must include the copyright and database right notice with it.

### What this means for us
| Use | Assessment |
|---|---|
| Analytics on ratings, recommendations, UPRN (portfolio piece, marts) | OGL: fine, with attribution |
| Landlord enters their own addresses; we match to a UPRN and show their own properties back | Plausibly within "effective management of properties with a view to promoting energy efficiency". **Confirm in writing** before any commercial launch |
| Commercial referrals (assessor/installer) using address fields | **Grey area.** The purpose is promoting energy efficiency in the rental market, but the business is commercial. **Email MHCLG and OS for confirmation before launch.** Mitigation: key everything on UPRN, and only display addresses the landlord supplied |
| Bulk marketing or cold outreach from register addresses | **No**: outside permitted purposes, and it also breaks our no-PII guardrail |

**Design rule:** store and join on `uprn`. Treat address and postcode fields as display-only for the landlord's own properties. Carry the copyright notice in any export (CSV and PDF) that contains address data.

## 4. Rate limits

- **6,000 requests per 5 minutes** per IP + bearer token combination. Going over returns HTTP 429; back off before retrying ([API technical docs](https://get-energy-performance-data.communities.gov.uk/api-technical-documentation)).
- Shared networks share the quota. The docs advise against batching requests; contact the team if you keep hitting the limit.
- This is plenty for our needs: a 50-property portfolio is about 100 calls (one search + one certificate fetch per property).

## 5. Fields

### Certificates (CSV data dictionary)
Downloaded from `/download/data-dictionary?property_type=domestic`. It has 91 fields; these are the ones we use:

| Field | Notes |
|---|---|
| `CERTIFICATE_NUMBER` | Primary key. Recommendation rows join on it (`rrn`). Replaces the old `LMK_KEY` |
| `UPRN`, `UPRN_SOURCE` | `UPRN_SOURCE` says whether the assessor or address matching supplied it. Older certificates may lack a UPRN |
| `CURRENT_ENERGY_RATING`, `POTENTIAL_ENERGY_RATING` | Band A–G |
| `CURRENT_ENERGY_EFFICIENCY`, `POTENTIAL_ENERGY_EFFICIENCY` | **Check against real data:** the new dictionary describes these as "£/m²/year", but historically they are the SAP score (1–100+). Check the value ranges after loading |
| `LODGEMENT_DATE`, `LODGEMENT_DATETIME`, `INSPECTION_DATE` | Certificate age; EPCs are valid for 10 years from lodgement |
| `TENURE` | owner-occupied / rented (social) / rented (private). Unreliable, so the landlord's input decides |
| `TRANSACTION_TYPE`, `REPORT_TYPE` | RdSAP (existing homes) vs SAP (new build) |
| `LOCAL_AUTHORITY` (ONS code), `LOCAL_AUTHORITY_LABEL` | **Bulk CSV only.** The API certificate JSON has no local-authority field (§7.12). On the API path the borough comes from the search filter `council[]=Newham`, stored as `raw.epc_certificate_index.council` |
| `PROPERTY_TYPE`, `BUILT_FORM`, `CONSTRUCTION_AGE_BAND`, `TOTAL_FLOOR_AREA` | Context; also inputs for the uplift fallback |
| `WALLS_/ROOF_/FLOOR_/WINDOWS_/MAINHEAT_/HOTWATER_…_DESCRIPTION` and `…_ENERGY_EFF` | Fabric and heating; used in explanations and the `needs_survey` heuristics |
| `MAINS_GAS_FLAG`, `MAIN_FUEL` | Heating-route context |

### Recommendations
The data dictionary doesn't cover these. Columns are taken from the warehouse view definition ([`db/migrate/20250716153536_alter_domestic_rr_views.rb`](https://github.com/communitiesuk/epb-data-warehouse/blob/main/db/migrate/20250716153536_alter_domestic_rr_views.rb)):

`rrn` (certificate number), `IMPROVEMENT_ITEM` (sequence), `IMPROVEMENT_ID`, `INDICATIVE_COST`, `IMPROVEMENT_SUMMARY_TEXT`, `IMPROVEMENT_DESCR_TEXT`

- **The bulk recommendations data has no per-measure score uplift.**
- **However:** the underlying lodged documents *do* hold a per-step `energy_performance_rating` (the cumulative SAP score after each measure) and an `environmental_impact_rating` inside `suggested_improvements`. This is visible in the warehouse's parsed-RdSAP test fixture. `GET /api/certificate` returns the full document JSON, so it should expose these fields. **Check with a real token** (see open questions).
- `INDICATIVE_COST` is free text on older certificates (e.g. "£500 - £1,500") and may be numeric on newer schemas. The staging layer must handle both.
- The dictionary page doesn't say whether the domestic full-load ZIP includes a recommendations file. Check on first download.

## 6. Update frequency
- The API is a live query of the register. `/api/deltas` gives changes between two dates.
- The pages read didn't state how often the bulk full-load files are refreshed. `/api/files/domestic/csv/info` returns file metadata, including when the file was generated.

## 7. Findings from live API probes (2026-11-06)

Seven certificate fetches across schema versions from 2009 to 2025:

1. **Per-step ratings: YES.** `GET /api/certificate` → `data.suggested_improvements[]` holds:
   - `sequence`
   - `improvement_type`, `improvement_category`
   - `improvement_details.improvement_number`
   - `indicative_cost`
   - **`energy_performance_rating`**: the cumulative SAP score after this step
   - `environmental_impact_rating`
   - Present on every schema checked: SAP-14.0 (2009), SAP-16.1, RdSAP-17.0, 18.0, 20.0.0 and 21.0.1 (2025).
   - Example: Band E home, score 41 → 68 (cavity wall insulation, £900–£1,200) → 78.
2. **`indicative_cost` formats vary:**
   - ranges, e.g. `"£4,000 - £14,000"`
   - single values, e.g. `"£1,000"`
   - `null` on at least one 2009 SAP certificate
   - Staging must handle all three. Missing cost → route confidence low.
3. **Potential score vs steps:** on that 2009 certificate, `energy_rating_potential` (67) was below the later step ratings (76, 86). This suggests the potential covers only the main measures, with "further measures" listed after it. Check `improvement_category` before treating every step as part of the main route.
4. **Scores:** `energy_rating_current` / `energy_rating_potential` are SAP scores (e.g. 41, 86), so the "£/m²" wording in the CSV dictionary is misleading for these fields.
5. **New builds** (SAP schema) can have no `suggested_improvements` at all (`null`).
6. **Search API:**
   - `council[]=Newham` works.
   - Rows carry `certificateNumber`, `uprn`, `currentEnergyEfficiencyBand`, `registrationDate`, `schemaType`, the address lines and `postcode`.
   - Results come newest first.
   - `tenure` in the certificate JSON is a code (e.g. `3`, `ND`); decode it via `/api/codes`.
7. **Newham volume:** 170,326 certificates since 2008, of which 75,027 are currently D–G (API counts).
8. **UPRN** was present on every certificate sampled, back to 2009.
9. **Bulk file:** the domestic full-load CSV ZIP (all of England and Wales) is **8.3 GB**, last updated 2026-11-01. That's too large for the dev machine (about 8.6 GB free), so see ADR-008.

10. **`improvement_category` decoded empirically** (first 4,297 Newham documents, 14,822 steps, 2026-11-06):
    - Category `3` steps (425): **always** score above `energy_rating_potential`. These are the "further measures" outside the potential rating.
    - Categories `1`, `2`, `5` (14,397): never score above the potential.
    - In 95.2% of certificates, the last non-category-3 step equals the potential score exactly.
    - **Engine rule:** the main route uses categories ≠ 3. Category 3 steps are optional "further measures", shown separately and flagged.
    - `improvement_type` is a letter code for the measure (e.g. `A` loft, `B` cavity wall, `Q` solid wall, `W1`/`W2` floor, `G` heating controls, `U` solar PV). Decode it via `/api/codes` in staging.

11. **UPRN coverage and address → UPRN match rate** (Newham certificate index, 2026-11-06; `uv run python -m lookup.match_rate --n 50 --seed 42`):
    - **Coverage:** 167,070 of 170,323 certificates (98.1%) carry a UPRN. 110,742 distinct UPRNs. A postcode holds a median of 26 UPRNs (p99 89, max 159).
    - **Matching** (`src/lookup/address_match.py`, ADR-010): exact postcode, then unit identifiers (house/flat numbers, flat letters, floor markers), then word overlap.
    - **Sample of 50 UPRNs × 4 typed variants** (exact, abbreviated + lowercase, building name dropped, terse): **200/200 correct, 0 wrong**. On 1,000 UPRNs: 3,984/4,000 correct (99.6%), 0 wrong, 16 ambiguous (a whole-building record beside its flats).
    - **Stress test: flat typed without its unit** ("26 High Street" for "Flat 23, …, 26 High Street"), 403 flats: 0 wrong, 366 ambiguous (ask which flat), 30 no match (the street has no number), 7 correct (only one unit on record).
    - **Caveat:** the variants are generated from register text, so this is an **upper bound**. Real input will add typos and wrong postcodes (`no_candidates`). Collect a few real typed addresses during landlord interviews to check this.

12. **No local-authority code in the certificate JSON** (45,000 Newham documents checked, 2026-11-06):
    - The top-level keys include no local authority, ONS code or constituency. No `E06`–`E09` code appears anywhere in the JSON (`E09000025` included).
    - The only geographic code is `region_code`, almost always `17` (some RdSAP 21 certificates have `1`). It appears to be the SAP climate region, not a borough.
    - **Borough identity:** it comes from the search API instead. All 170,323 index rows carry `council = 'Newham'`, because the crawl filters on `council[]`. For a second borough (Tower Hamlets), crawl it separately and use `raw.epc_certificate_index.council`; don't derive it from the document.

## 8. Still open
1. Bearer token lifetime and refresh process.
2. Meaning of categories 1 / 2 / 5 (all inside the potential rating, so the engine doesn't need it).
3. ~~ONS code for Newham in the certificate JSON~~: resolved, there isn't one (§7.12).
4. Written confirmation from MHCLG/OS about address-data use for a commercial landlord tool. Send before any commercial launch; not needed for the portfolio build. Draft: `docs/licensing_enquiry_draft.md` (not sent).

Contact: mhclg.digital-services@communities.gov.uk (prefix the subject with "EPB").
