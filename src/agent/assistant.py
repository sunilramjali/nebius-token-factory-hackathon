"""Tool-calling assistant on Nebius Token Factory (OpenAI-compatible API).

Code does the maths, the LLM explains. Guardrails in code, not just in the prompt:
  - the model only sees tool output (no address fields; see agent/tools.py);
  - every £ amount in a reply must appear in this turn's tool output, else the model gets one
    retry and then the reply is flagged;
  - every reply ends with the indicative-only label, added here, not by the model.

Usage (terminal chat, for testing before the app exists):
  uv run --env-file .env python -m agent.assistant --uprn 100023... --uprn ...
  uv run --env-file .env python -m agent.assistant --csv data/portfolios/mine.csv
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from agent.tools import TOOL_SPECS, Toolbox

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DISCLAIMER = "Indicative — a survey by an accredited assessor is required. Not legal advice."
UNCHECKED_NOTE = (
    "Some amounts above couldn't be matched to the engine's figures. Check them in the table."
)
FALLBACK = "Sorry, I couldn't work that out from the engine's results. Check the table above."
MAX_TOOL_ROUNDS = 6
LOOK_IT_UP = (
    "You didn't call any tool. Look the facts up with your tools first, then answer. "
    "If no tool is needed, just answer again."
)
# Per reply. Reasoning models spend part of it thinking; the eval uses the same value.
MAX_TOKENS = 800

SYSTEM_PROMPT = """\
You help a UK landlord understand which of their rental homes are below EPC C and the \
cheapest way to reach C. You explain results; you never calculate them.

Facts:
- Use only numbers, dates and statuses returned by your tools. Never estimate, add up, round \
or invent a cost, rating or date. If a tool doesn't give it, say you don't know.
- Call get_portfolio_summary before answering about the landlord's homes. Call get_property \
for detail on one home. Call get_rules for deadlines and the cost cap.
- For "who do I contact?" call find_help. Name the trades and where to find them. Never \
name or recommend a specific company.
- For "make my plan", "sort out <home>" or "do it all for me", call make_plan (with the home \
if one is named). Then say in 2 or 3 short sentences how many homes are planned, how many \
messages are ready, and which home to start with. The app shows the plan below.
- Only when the landlord asks for a quote request or message, call draft_quote_request. \
The app shows the full message \
below your reply: say it's below in one sentence. Don't rewrite it.
- Earlier replies in this chat may be wrong or out of date. Always look figures up again with \
a tool; never repeat a cost or rating from an earlier message.
- Start with a one-sentence answer, then the details.
- Refer to homes by their label, not by UPRN.
- Never mention tools, "the tool" or "the data" to the landlord. Say the facts directly.
- When a tool says what a figure covers, use that description. Don't describe it yourself.
- On exemptions, repeat only the tool's "exemption" wording. Never add conditions or criteria.

Wording (the reader is busy and not technical):
- Short sentences, 25 words at most. Lead with the answer. Plain English.
- Say "rating" or "EPC rating", never "SAP" or "EER". Say "rating gain", never "uplift" or \
"points". Say "measure", not "recommendation".
- The 2030 rules are proposals: write "would", "should" or "under the proposals", never \
"must" or "will".
- Never give a legal conclusion. Say "likely over the cap" or "you may be able to register an \
exemption", never "you are exempt" or "you will be compliant".
- Never say a home is, isn't or is not currently exempt. Give its status and the tool's \
exemption wording instead.
- Don't add a disclaimer; one is added for you.
- Keep answers under 150 words unless asked for more.
"""


@dataclass
class Reply:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # name, args, result
    grounded: bool = True


_GBP_RE = re.compile(r"£\s?(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", re.I)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def ungrounded_amounts(text: str, tool_results: list[Any]) -> list[str]:
    """£ amounts in `text` that appear nowhere in the tool results (dicts or JSON strings)."""
    decoded = []
    for r in tool_results:
        try:  # JSON strings may escape £ as £, which glues "a3" onto the amount
            decoded.append(json.loads(r) if isinstance(r, str) else r)
        except json.JSONDecodeError:
            decoded.append(r)
    known = {_num(m) for m in _NUM_RE.findall(json.dumps(decoded, ensure_ascii=False))}
    bad = []
    for m in _GBP_RE.finditer(text):
        value = _num(m.group(1)) * (1000 if m.group(2) else 1)
        if value not in known:
            bad.append(m.group(0).strip())
    return bad


# Legal conclusions the reply must never contain (docs/report_style.md, "Legal and compliance
# wording"). The prompt asks for this too, but Qwen still wrote "Home 4 is not exempt now".
LEGAL_PATTERNS = [
    r"(?i)\b(you are|you're|is|are|not|isn't|aren't)(\s+\w+)?\s+exempt\b",
    r"(?i)\bwill be compliant\b",
    r"(?i)\bmust\b",
    r"(?i)\bwill (have|need) to\b",
    r"(?i)\bguarantee",
]


# Talking about the machinery ("The tool states: ...") reads oddly to a landlord. The prompt
# asks for plain facts, but Qwen kept doing it, so it also gets one retry. Cosmetic: never
# dropped.
TOOL_TALK = re.compile(r"(?i)\b(the|my) (tools?|data)\b")


# The draft tool only runs when the landlord asks for a message: Qwen drafted unasked-for
# quote requests (e.g. for "How much would a heat pump cost?").
DRAFT_REQUEST = re.compile(
    r"(?i)\b(quotes?|message|email|e-mail|letter|draft|write|contact them|plan|sort out|"
    r"sort (it|this|everything|them) out|do it|handle|take care|prepare|get (it|this|them) done)\b"
)


def wants_draft(question: str) -> bool:
    return bool(DRAFT_REQUEST.search(question or ""))


# Lead-ins like "The tool states: " that survive the retry are cut; the facts after them stay.
TOOL_LEAD = re.compile(
    r"(?i)\b(the|my) (tools?|data)( for [^.:]{1,40})? (states?|says?|shows?|wording is)[:,]?\s*"
)


def strip_tool_talk(text: str) -> str:
    def cap(m: re.Match) -> str:  # capitalise the start of a sentence left behind
        return m.group(1) + m.group(2).upper()

    out = TOOL_LEAD.sub("", text)
    out = re.sub(r'(^|[.!?]\s+|\n)["“]?([a-z])', cap, out)
    return out


# "Did the agent do what it said?" A reply may only say a message is ready if a draft was
# actually made this turn, and it may never say it sent, emailed or booked anything: the app
# never does. (Qwen said "the full quote request is below" after its draft was blocked.)
DRAFT_CLAIMS = [
    r"(?i)\b(quote requests?|messages?|drafts?|emails?|letters?)\b[^.!?\n]{0,60}"
    r"\b(below|ready|drafted|written)\b",
    r"(?i)\b(it's|it is|shown|see it|find it)\s+below\b(?!\s+(c\b|band|the|£|\d))",
]
SEND_CLAIMS = [
    r"(?i)\b(i've|i have|we've|we have|i|we)\s+(just\s+)?"
    r"(sent|emailed|e-mailed|contacted|booked)\b",
    r"(?i)\b(has|have) been (sent|emailed|booked)\b",
]


def unbacked_claims(text: str, calls: list[dict[str, Any]]) -> list[str]:
    """Action claims in `text` with no tool evidence in this turn's calls."""
    drafted = any(
        (c["name"] == "draft_quote_request" and "body" in (c.get("result") or {}))
        or (c["name"] == "make_plan" and (c.get("result") or {}).get("messages_ready"))
        for c in calls
    )
    pats = SEND_CLAIMS + ([] if drafted else DRAFT_CLAIMS)
    return sorted({m.group(0) for p in pats for m in re.finditer(p, text)})


def tool_talk(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in TOOL_TALK.finditer(text)})


def legal_conclusions(text: str) -> list[str]:
    return sorted({m.group(0) for pat in LEGAL_PATTERNS for m in re.finditer(pat, text)})


def drop_sentences(text: str, patterns: list[str]) -> str:
    """Remove each sentence that matches any pattern; keep line breaks and the rest."""
    lines = []
    for line in text.split("\n"):
        parts = re.split(r"(?<=[.!?])\s+", line)
        kept = [s for s in parts if not any(re.search(p, s) for p in patterns)]
        if kept or not line.strip():
            lines.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def lead_break(text: str) -> str:
    """Put the first sentence, the takeaway, on its own line, with the details below it."""
    head, sep, rest = text.partition("\n")
    m = re.search(r"(?<=[.!?])\s+(?=[A-Z£\"“(])", head)
    if not m:
        return text
    out = f"{head[: m.start()]}\n\n{head[m.end() :]}"
    return out + (sep + rest if sep else "")


def make_client():
    from openai import OpenAI

    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        raise SystemExit("NEBIUS_API_KEY is not set (copy .env.example to .env)")
    return OpenAI(base_url=os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL), api_key=key)


class Assistant:
    def __init__(
        self, toolbox: Toolbox, client=None, model: str | None = None, max_tokens: int = MAX_TOKENS
    ):
        self.toolbox = toolbox
        self.max_tokens = max_tokens  # reasoning models spend part of this on thinking
        self.client = client if client is not None else make_client()
        self.model = model or os.environ.get("NEBIUS_MODEL")
        if not self.model:
            raise SystemExit("NEBIUS_MODEL is not set (pick a tool-calling model)")
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Running totals across the chat, for cost tracking (scripts/eval_models.py).
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._question = ""  # the landlord's current question (gates the draft tool)

    def _complete(self) -> Any:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=TOOL_SPECS,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=self.max_tokens,
        )
        self.usage["requests"] += 1
        if (u := getattr(resp, "usage", None)) is not None:
            self.usage["prompt_tokens"] += u.prompt_tokens or 0
            self.usage["completion_tokens"] += u.completion_tokens or 0
        return resp.choices[0].message

    def _run(self, calls: list[dict[str, Any]]) -> str | None:
        """Model/tool loop until the model answers in text. None if it never does."""
        for _ in range(MAX_TOOL_ROUNDS):
            msg = self._complete()
            if not msg.tool_calls:
                text = (msg.content or "").strip()
                self.messages.append({"role": "assistant", "content": text})
                return text or None
            self.messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if tc.function.name in ("draft_quote_request", "make_plan") and not wants_draft(
                        self._question
                    ):
                        result = {
                            "error": "The landlord didn't ask for a message. Don't draft one."
                        }
                    else:
                        result = self.toolbox.call(tc.function.name, args)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    args, result = {}, {"error": "arguments must be a JSON object"}
                calls.append({"name": tc.function.name, "args": args, "result": result})
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        return None

    def ask(self, question: str) -> Reply:
        self._question = question
        self.messages.append({"role": "user", "content": question})
        calls: list[dict[str, Any]] = []
        text = self._run(calls)
        if not calls:  # answered without looking anything up: one nudge (Qwen said "I don't
            # know the cost for Flat C" mid-conversation instead of calling get_property)
            self.messages.append({"role": "user", "content": LOOK_IT_UP})
            text = self._run(calls) or text
        if text is None:
            return Reply(f"{FALLBACK}\n\n{DISCLAIMER}", calls, grounded=False)

        def results() -> list[str]:  # amounts may come from earlier turns of this chat too
            return [m["content"] for m in self.messages if m["role"] == "tool"]

        bad, legal = ungrounded_amounts(text, results()), legal_conclusions(text)
        talk, claims = tool_talk(text), unbacked_claims(text, calls)
        if bad or legal or talk or claims:  # one retry covers every problem
            fixes = []
            if bad:
                fixes.append(f"These amounts aren't in the tool results: {', '.join(bad)}.")
            if legal:
                fixes.append(
                    f"These read as legal conclusions: {', '.join(legal)}. Give the home's "
                    "status and the tool's wording instead."
                )
            if talk:
                fixes.append(
                    f"Don't mention {', '.join(talk)}. Say the facts directly to the landlord."
                )
            if claims:
                fixes.append(
                    f"These claims have no tool result behind them: {', '.join(claims)}. Only "
                    "say a message is ready after draft_quote_request succeeds. The app never "
                    "sends, emails or books anything."
                )
            self.messages.append({"role": "user", "content": " ".join(fixes) + " Answer again."})
            retry = self._run(calls)
            if retry is not None:
                text = retry
                bad, legal = ungrounded_amounts(text, results()), legal_conclusions(text)
                claims = unbacked_claims(text, calls)
        if claims:  # still there after the retry: those sentences go
            text = drop_sentences(text, SEND_CLAIMS + DRAFT_CLAIMS) or FALLBACK
        if tool_talk(text):  # still there after the retry: cut the lead-ins, keep the facts
            text = strip_tool_talk(text)
        if legal:  # still there after the retry: the landlord never sees those sentences
            text = drop_sentences(text, LEGAL_PATTERNS) or FALLBACK
        text = lead_break(text)
        grounded = not bad
        if not grounded:
            text = f"{text}\n\n{UNCHECKED_NOTE}"
        return Reply(f"{text}\n\n{DISCLAIMER}", calls, grounded)


def main(argv: list[str] | None = None) -> None:
    import duckdb
    from dotenv import load_dotenv

    from engine.policy import load_policy
    from lookup.portfolio import resolve_csv

    load_dotenv()
    p = argparse.ArgumentParser(prog="agent.assistant")
    p.add_argument("--target", default=os.environ.get("DUCKDB_TARGET", "data/epc.duckdb"))
    p.add_argument("--uprn", type=int, action="append", default=[])
    p.add_argument("--csv", help="landlord portfolio CSV (address, postcode, label, uprn)")
    args = p.parse_args(argv)

    con = duckdb.connect(args.target, read_only=True)
    labels = {u: f"Home {i}" for i, u in enumerate(args.uprn, start=1)}
    if args.csv:
        for r in resolve_csv(con, args.csv):
            if r.status in ("matched", "given"):
                labels[r.uprn] = r.row.label
            else:
                print(f"line {r.row.line} ({r.row.label}): {r.status}, skipped")
    if not labels:
        raise SystemExit("give --uprn ... or --csv")
    bot = Assistant(Toolbox.from_db(con, labels, load_policy()))
    print(f"{len(bot.toolbox.props)} homes loaded. Ask a question (Ctrl-D to quit).")
    while True:
        try:
            q = input("\n> ").strip()
        except EOFError:
            break
        if q:
            reply = bot.ask(q)
            print(f"  [tools: {', '.join(c['name'] for c in reply.tool_calls) or 'none'}]")
            print(reply.text)


if __name__ == "__main__":
    main()
