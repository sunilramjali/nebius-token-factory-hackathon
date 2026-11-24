"""Compare Nebius models on the assistant: rule-following, grounding, speed and cost.

Runs a fixed set of landlord questions against a fixed 6-home portfolio from the local
snapshot (one home per status), once per model, and scores every answer automatically:
  - answered (no fallback), used a tool;
  - grounded: every £ amount appears in tool output (agent.assistant.ungrounded_amounts);
  - wording: no banned words or legal conclusions, sentences of 25 words or fewer
    (docs/report_style.md; same rules as tests/test_report_wording.py).
Cost comes from the token counts the API returns and the prices from /v1/models?verbose=true.

What goes to Nebius: UPRNs, labels "Home 1".."Home 6" and engine figures. No addresses.
Results are written to data/evals/ (gitignored).

Usage:
  uv run --env-file .env python scripts/eval_models.py --dry-run          # cost estimate only
  uv run --env-file .env python scripts/eval_models.py --model openai/gpt-oss-120b
  uv run --env-file .env python scripts/eval_models.py --all
  uv run --env-file .env python scripts/eval_models.py --repeat 3          # stability
  uv run --env-file .env python scripts/eval_models.py --questions my_demo_questions.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import duckdb

from agent.assistant import (
    DEFAULT_BASE_URL,
    DISCLAIMER,
    FALLBACK,
    LEGAL_PATTERNS,
    MAX_TOKENS,
    Assistant,
    make_client,
)
from agent.tools import Toolbox
from engine.policy import load_policy
from webapp.logic import DEMO_PORTFOLIO_SQL

OUT_DIR = Path("data/evals")
MAX_WORDS = 25


# (id, question, what it probes)
QUESTIONS = [
    ("priority", "Which of my homes should I deal with first, and why?", "summary + ranking"),
    ("cost_one", "How much would it cost to get Home 2 to C, and what works are involved?",
     "detail, borderline caveat"),
    ("exempt_trap", "Is Home 4 exempt from the rules?", "no legal conclusion"),
    ("law_trap", "Do I have to get my homes to C by law? By when?", "proposal wording"),
    ("total", "What will all my homes cost in total?", "no own maths"),
    ("unknown", "How much would a heat pump cost for Home 1?", "no invented costs"),
]  # fmt: skip

# Same as tests/test_report_wording.py, plus the assistant's own legal-conclusion patterns.
# Lowercase "point" is allowed: the report itself says "a point or two short".
BANNED = [
    r"\bSAP\b", r"\bEER\b", r"\bmid-?point\b", r"\buplift", r"\bPoints\b", *LEGAL_PATTERNS,
]  # fmt: skip


def wording_problems(text: str) -> tuple[list[str], list[str]]:
    """(banned phrases found, sentences over MAX_WORDS) in the model's own text."""
    body = text.replace(DISCLAIMER, "")
    body = re.sub(r"[*_`#>|]", " ", body)  # markdown
    banned = sorted({m.group(0) for pat in BANNED for m in re.finditer(pat, body)})
    long = []
    for block in body.split("\n"):
        for s in re.split(r"(?<=[.!?])\s+", block.strip()):
            if len(s.split()) > MAX_WORDS:
                long.append(s)
    return banned, long


def fetch_models() -> list[dict]:
    req = urllib.request.Request(
        os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL).rstrip("/") + "/models?verbose=true",
        headers={"Authorization": f"Bearer {os.environ['NEBIUS_API_KEY']}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def price(m: dict) -> tuple[float, float]:
    """USD per token (input, output)."""
    p = m.get("pricing") or {}
    return float(p.get("prompt") or 0), float(p.get("completion") or 0)


def run_model(
    model: str,
    toolbox: Toolbox,
    prices: tuple[float, float],
    max_tokens: int,
    questions: list[tuple[str, str, str]],
):
    client = make_client()
    answers = []
    for qid, question, probe in questions:
        bot = Assistant(toolbox, client=client, model=model, max_tokens=max_tokens)
        t = time.monotonic()
        try:
            reply = bot.ask(question)
            error = None
        except Exception as e:  # rate limit, bad request, timeout: record, keep going
            reply, error = None, f"{type(e).__name__}: {str(e)[:200]}"
        secs = time.monotonic() - t
        text = reply.text if reply else ""
        banned, long = wording_problems(text) if reply else ([], [])
        answered = reply is not None and not text.startswith(FALLBACK)
        used_tool = bool(reply and reply.tool_calls)
        grounded = bool(reply and reply.grounded)
        cost = bot.usage["prompt_tokens"] * prices[0] + bot.usage["completion_tokens"] * prices[1]
        answers.append(
            {
                "question": qid,
                "probe": probe,
                "passed": answered and used_tool and grounded and not banned and not long,
                "answered": answered,
                "used_tool": used_tool,
                "tools": [c["name"] for c in reply.tool_calls] if reply else [],
                "grounded": grounded,
                "banned": banned,
                "long_sentences": long,
                "seconds": round(secs, 2),
                "usage": bot.usage,
                "cost_usd": cost,
                "error": error,
                "text": text,
            }
        )
    return model, answers


def summarise(model: str, answers: list[dict]) -> dict:
    n = len(answers)
    cost = sum(a["cost_usd"] for a in answers)
    return {
        "model": model,
        "passed": sum(a["passed"] for a in answers),
        "of": n,
        "grounded": sum(a["grounded"] for a in answers),
        "wording_ok": sum(not a["banned"] and not a["long_sentences"] for a in answers),
        "errors": sum(a["error"] is not None for a in answers),
        "avg_seconds": round(sum(a["seconds"] for a in answers) / n, 1),
        "cost_usd": cost,
        "usd_per_1000_questions": cost / n * 1000,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", action="append", default=[])
    p.add_argument("--all", action="store_true", help="every chat model that supports tools")
    p.add_argument("--dry-run", action="store_true", help="estimate cost, call no models")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help="default: same as the app")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--repeat", type=int, default=1, help="ask every question N times")
    p.add_argument(
        "--questions", help="text file, one question per line (rehearse your demo questions)"
    )
    p.add_argument("--target", default=os.environ.get("DUCKDB_TARGET", "data/epc.duckdb"))
    args = p.parse_args()
    questions = QUESTIONS
    if args.questions:  # rehearsal: no traps, so read the answers yourself
        lines = [q.strip() for q in Path(args.questions).read_text().splitlines() if q.strip()]
        questions = [(f"q{i}", q, "rehearsal") for i, q in enumerate(lines, start=1)]
    # Repeats show how stable a model is: one run of 6 questions is thin evidence.
    questions = [(f"{qid}#{k}" if args.repeat > 1 else qid, q, probe)
                 for k in range(1, args.repeat + 1) for qid, q, probe in questions]  # fmt: skip

    catalog = {m["id"]: m for m in fetch_models()}
    if args.all:
        models = sorted(
            i for i, m in catalog.items() if "tools" in (m.get("supported_features") or [])
        )
    else:
        models = args.model or [os.environ.get("NEBIUS_MODEL") or "openai/gpt-oss-120b"]
    unknown = [m for m in models if m not in catalog]
    if unknown:
        raise SystemExit(f"not in the catalog: {unknown}")

    if args.dry_run:
        # Per question, measured on gpt-oss-120b (~2,400 in / ~350 out), padded for
        # models that think longer or call more tools.
        tok_in, tok_out = 5000, 1500
        total = 0.0
        for m in models:
            pin, pout = price(catalog[m])
            c = len(questions) * (tok_in * pin + tok_out * pout)
            total += c
            print(f"{m:45s} ~${c:.4f}")
        print(f"{len(models)} models x {len(questions)} questions: ~${total:.2f} total")
        return

    con = duckdb.connect(args.target, read_only=True)
    labels = {uprn: label for label, uprn in con.execute(DEMO_PORTFOLIO_SQL).fetchall()}
    toolbox = Toolbox.from_db(con, labels, load_policy())
    n_q = len(questions)
    print(f"portfolio: {len(toolbox.props)} homes; {len(models)} models x {n_q} questions")

    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [
            pool.submit(run_model, m, toolbox, price(catalog[m]), args.max_tokens, questions)
            for m in models
        ]
        for f in futs:
            model, answers = f.result()
            results[model] = answers
            s = summarise(model, answers)
            print(f"  done {model}: {s['passed']}/{s['of']} passed, ${s['cost_usd']:.4f}")

    summary = sorted(
        (summarise(m, a) for m, a in results.items()),
        key=lambda s: (-s["passed"], s["usd_per_1000_questions"]),
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    report = {"max_tokens": args.max_tokens, "summary": summary, "answers": results}
    out.write_text(json.dumps(report, indent=1))

    print(f"\n{'model':45s} pass  ground wording err  secs   $run    $/1k q")
    for s in summary:
        print(
            f"{s['model']:45s} {s['passed']}/{s['of']}   {s['grounded']}/{s['of']}    "
            f"{s['wording_ok']}/{s['of']}    {s['errors']}  {s['avg_seconds']:5.1f}  "
            f"{s['cost_usd']:.4f}  {s['usd_per_1000_questions']:.2f}"
        )
    print(f"\nTotal: ${sum(s['cost_usd'] for s in summary):.4f}. Answers: {out}")


if __name__ == "__main__":
    main()
