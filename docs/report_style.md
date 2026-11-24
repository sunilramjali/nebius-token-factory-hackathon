# Report style guide

How every landlord-facing sentence is written: reports, the future app, and agent replies. `tests/test_report_wording.py` enforces the testable rules; the `report-wording` skill covers the rest.

## Who reads it
A landlord with 5–50 homes. Busy, not technical, often reading on a phone or a printed PDF. They may forward it to an accountant, broker or assessor.

## Plain English
- Sentences of **25 words or fewer** (enforced). Aim for 15.
- One idea per sentence. Lead with the point: "Fix early: about £400–£950."
- Everyday words: "about" not "approximately", "get" not "obtain", "home" not "dwelling".
- Use numerals and £ signs: "£4,500", "2 points".
- Use the GOV.UK style guide when unsure.

## Words we don't use
| Avoid | Use instead |
|---|---|
| SAP, EER | "EPC rating" or "rating" |
| mid-point | "the middle of the cost range" |
| cap-counting | "counts toward the £10,000 cap" |
| data confidence | a plain reason: "recent certificate, full cost data" |
| uplift, points | "rating gain" |
| recommendation (as a noun for works) | "measure" |

## Legal and compliance wording (never break these)
- The 2030 rules are **proposals**: write "would", "should" or "under the proposals", never "must" or "will".
- Never state a legal conclusion. Use "likely over the cap" and "you may be able to register an exemption"; never "you are exempt" or "you will be compliant".
- Every number is indicative. Every report carries the disclaimer: survey by an accredited assessor required; not legal or compliance advice.
- Dates and amounts (2030 deadline, 2029 lock-in, £10,000 cap, £100,000 value threshold) come from `config/policy.yaml`, never typed into templates (enforced).
- Don't predict the outcome of unfinished policy. For example, "the 2030 heating standard isn't finalised" is fine; "a boiler won't comply" is not.
- Mention a referral or fee relationship once one exists (`docs/owner_todo.md`).

## Status labels (one per home)
| Colour | Label | Meaning |
|---|---|---|
| green | Fix early | C is likely within the cap |
| amber | Fix early (borderline) | cheapest route only just reaches C |
| amber | Fix early (needs a boiler) | within the cap only with a new gas or oil boiler |
| amber | Fix early (check quotes) | cost range spans the cap |
| red | Likely over the cap | no route is likely within the cap |
| red | Can't reach C | the certificate's measures don't reach C |
| grey | Survey first | costs missing or certificate too old |
| green | Already C | no action needed now |

Each label comes with **one next action** in plain words.

## Layout
1. Title, date, disclaimer.
2. **What to do next:** up to 5 numbered actions for the whole portfolio.
3. Summary table with a status per home.
4. Rules in brief, worded as proposals.
5. One card per home: status and a one-line cost, a short reason, measures table, safer or boiler options.
