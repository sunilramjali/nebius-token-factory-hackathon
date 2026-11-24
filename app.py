"""Streamlit demo: landlord enters homes -> results -> chat with the assistant.

    uv run --env-file .env streamlit run app.py              # full app, local snapshot
    DEMO_MODE=1 uv run --env-file .env streamlit run app.py  # what the hosted demo shows

Numbers come from the engine; the assistant only explains them. Register addresses are shown
only to settle an ambiguous match and never reach Nebius.

Demo mode (the public Streamlit Cloud app): on whenever the local snapshot is missing, so a
hosted copy can never reach register data. It serves the 6 homes in
examples/demo_portfolio.json (no addresses), has no address entry, and caps chat questions.

Look and feel: src/webapp/view.py (Apple-like styling, stat tiles, one meter per home).
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))  # hosts that don't install it

import duckdb
import streamlit as st
from dotenv import load_dotenv

from agent.assistant import Assistant
from agent.help import draft_quote_request, fill_draft, find_help, mailto
from agent.tools import Toolbox, property_facts
from engine.policy import load_policy
from report.portfolio_report import DISCLAIMER, LICENCE, render
from webapp import view
from webapp.logic import (
    DEMO_LANDLORD,
    DEMO_QUESTIONS_PER_DAY,
    DEMO_QUESTIONS_PER_VISITOR,
    MAX_QUESTION_CHARS,
    DailyCounter,
    build_toolbox,
    demo_entries,
    follow_ups,
    load_demo,
    needs_input_message,
    resolve_rows,
    rows_from_entries,
    table_rows,
    text,
    typed_addresses,
)

load_dotenv()
st.set_page_config(page_title="Route to EPC C", page_icon="🏠", layout="centered")
POLICY = load_policy()
EMPTY_ROWS = [{"label": "", "address": "", "postcode": "", "uprn": ""} for _ in range(3)]
DB_PATH = os.environ.get("DUCKDB_TARGET", "data/epc.duckdb")
DEMO = os.environ.get("DEMO_MODE") == "1" or not Path(DB_PATH).is_file()


def t(key: str, **extra) -> str:
    return text(key, POLICY, **extra)


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)  # markup built in view.py, user text escaped


def secret(name: str) -> str | None:
    """Env var, else a Streamlit Cloud secret (never stored in the repo)."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets file locally
        return None
    if value:
        os.environ[name] = str(value)  # the assistant reads the environment
    return value


@st.cache_resource
def daily_counter() -> DailyCounter:
    return DailyCounter(DEMO_QUESTIONS_PER_DAY)  # shared by every visitor of this server


@st.cache_resource
def connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(DB_PATH, read_only=True)


def con() -> duckdb.DuckDBPyConnection:
    return connection().cursor()  # one cursor per use: Streamlit runs scripts in threads


def check(entries: list[dict]) -> None:
    rows = rows_from_entries(entries)
    if not rows:
        st.session_state.pop("toolbox", None)
        st.warning(t("no_homes"))
        return
    resolved = resolve_rows(con(), rows)
    st.session_state.resolved = resolved
    toolbox = build_toolbox(con(), resolved, POLICY)
    for r in resolved:  # found on the register but no engine result (as report_from_csv does)
        if r.uprn is not None and str(r.uprn) in toolbox.missing:
            r.status = "no_data"
    st.session_state.toolbox = toolbox
    st.session_state.home_addresses = typed_addresses(resolved)  # what the landlord typed
    st.session_state.chat = []
    st.session_state.pop("bot", None)


def details_box() -> None:
    """The landlord's name and phone, typed once and used in every message (pre-filled with
    fictional examples in the demo). Kept in this browser session; never sent to the AI."""
    with st.expander(t("your_details"), expanded=not ss.get("me_name")):
        c1, c2 = st.columns(2)
        c1.text_input(t("form_name"), key="me_name")
        c2.text_input(t("form_phone"), key="me_phone")
        st.caption(t("details_example") if DEMO else t("details_note"))


def draft_box(d: dict, key: str) -> None:
    """A finished message: the landlord's saved details and the address they typed for this
    home are filled in by code, so it's one click to copy or email. Never sent to the AI;
    never the register's address."""
    p = ss.toolbox.home(d.get("home", ""))
    known = ss.get("home_addresses", {}).get(p.property_id, "") if p else ""
    if known:
        address = known
        st.caption(t("form_address_known", address=known))
    else:
        address = st.text_input(t("form_address"), key=f"{key}_address")
    body = fill_draft(d["body"], ss.get("me_name", ""), address, ss.get("me_phone", ""))
    st.code(f"Subject: {d['subject']}\n\n{body}", language=None)  # has a copy button
    st.link_button(t("open_email"), mailto(d["subject"], body))
    if not ss.get("me_name"):
        st.caption(t("details_missing"))
    st.caption(t("form_note"))


def plan_view(plan: dict, key: str) -> None:
    """The agent's plan: one card per home in priority order, then a message per contact.
    Messages are rebuilt here by code, so the model never sees or edits them."""
    for h in plan["homes"]:
        html(view.plan_home(h))
        p = ss.toolbox.home(h["uprn"])
        for n in h["contacts_in_order"]:
            with st.expander(t("plan_message_to", who=n["who"])):
                d = draft_quote_request(p, POLICY, n["trade"])
                draft_box(d, f"{key}_{h['uprn']}_{n['trade']}")


def offer_draft(home_id: str, trade: str, who: str) -> None:
    """A "Write to ..." next step: built by code, no AI call, not counted as a question."""
    p = ss.toolbox.home(home_id)
    d = draft_quote_request(p, POLICY, trade)
    call = {"name": "draft_quote_request", "args": {"home": p.label, "trade": trade}, "result": d}
    ss.chat.append(("assistant", t("draft_ready", who=who), [call]))
    st.rerun()


def ask(question: str) -> None:
    """Send one question to the assistant, within the demo limits."""
    if len(question) > MAX_QUESTION_CHARS:
        st.warning(t("too_long", n=MAX_QUESTION_CHARS))
        return
    if DEMO and sum(r == "user" for r, _, _ in ss.get("chat", [])) >= DEMO_QUESTIONS_PER_VISITOR:
        st.info(t("chat_limit", n=DEMO_QUESTIONS_PER_VISITOR))
        return
    if DEMO and not daily_counter().take():
        st.warning(t("chat_daily"))
        return
    if "bot" not in ss:
        ss.bot = Assistant(ss.toolbox)
    ss.setdefault("chat", [])
    ss.chat.append(("user", question, []))
    with st.spinner(""):
        try:
            reply = ss.bot.ask(question)
            ss.chat.append(("assistant", reply.text, reply.tool_calls))
        except Exception:  # network, rate limit: the results still work
            ss.chat.append(("assistant", t("chat_error"), []))
    st.rerun()


ss = st.session_state
ss.setdefault("entries", EMPTY_ROWS)
ss.setdefault("me_name", DEMO_LANDLORD["name"] if DEMO else "")
ss.setdefault("me_phone", DEMO_LANDLORD["phone"] if DEMO else "")
ss.setdefault("editor_key", 0)
html(view.CSS)

# --- Hero ---------------------------------------------------------------------------------
html(view.hero(t("title"), t("subtitle"), t("demo_banner") if DEMO else None))
if DEMO:
    html(
        view.pitch(
            [
                (t("user_h"), t("user")),
                (t("problem_h"), t("problem")),
                (t("solution_h"), t("solution")),
            ]  # fmt: skip
        )
    )
    if "toolbox" not in ss:
        ss.toolbox = Toolbox(load_demo(), POLICY)
        ss.chat = []

# --- Enter homes (full app only) -----------------------------------------------------------
if not DEMO:
    html(view.section(t("add_homes")))
    st.caption(t("entry_help"))
    edited = st.data_editor(
        ss.entries,
        num_rows="dynamic",
        width="stretch",
        key=f"editor_{ss.editor_key}",
        column_config={
            "label": st.column_config.TextColumn("Your name for it"),
            "address": st.column_config.TextColumn("Address", width="large"),
            "postcode": st.column_config.TextColumn("Postcode"),
            "uprn": st.column_config.TextColumn("UPRN (if you know it)"),
        },
    )
    c1, c2, _ = st.columns([1.2, 1.2, 3])
    if c1.button(t("check"), type="primary"):
        ss.entries = edited
        check(edited)
    if c2.button(t("demo")):
        ss.entries = demo_entries(con())
        ss.editor_key += 1  # a new key makes the editor show the new rows
        check(ss.entries)
        st.rerun()

# --- Rows that need input (full app only) ----------------------------------------------------
resolved = [] if DEMO else ss.get("resolved", [])
problems = [r for r in resolved if r.status not in ("matched", "given")]
if problems:
    html(view.section(t("needs_input")))
    for r in problems:
        r_label = f"{r.row.label} ({r.row.address})" if r.row.address else r.row.label
        st.markdown(f"**{r_label}**: {needs_input_message(r.status)}")
        if r.status == "ambiguous" and 1 < len(r.options) <= 8:
            # Register addresses, shown only to the landlord who typed this home.
            choice = st.selectbox(
                t("pick"),
                [None, *r.options],
                format_func=lambda o: "—" if o is None else o[1],
                key=f"pick_{r.row.line}",
            )
            if choice is not None:
                ss.entries = [
                    {**e, "uprn": str(choice[0])} if i == r.row.line - 1 else e
                    for i, e in enumerate(
                        [e for e in ss.entries if (e.get("address") or e.get("uprn"))]
                    )
                ]
                ss.editor_key += 1
                check(ss.entries)
                st.rerun()

# --- Results -------------------------------------------------------------------------------
toolbox = ss.get("toolbox")
if toolbox and toolbox.props:
    html(view.section(t("results")))
    html(
        view.tiles(
            toolbox,
            {
                "below": t("tile_below"),
                "fix_early": t("tile_fix_early"),
                "over_cap": t("tile_over_cap"),
                "spend": t("tile_spend"),
                "spend_note": t("tile_spend_note"),
            },
        )
    )
    # The autonomous step: one click plans every home and prepares every message.
    needs_work = any(p.current_score < POLICY.band_c_threshold for p in toolbox.props.values())
    if needs_work and secret("NEBIUS_API_KEY") and secret("NEBIUS_MODEL"):
        c1, c2 = st.columns([1, 2.4], vertical_alignment="center")
        if c1.button(t("make_plan"), type="primary", key="make_plan", width="stretch"):
            ask(t("plan_question"))
        c2.caption(t("plan_hint"))
    details_box()

    for p in toolbox.props.values():
        html(view.home_card(p, toolbox))
        f = property_facts(p, POLICY)
        route = f.get("cheapest_route")
        extras = [line for line in [*f.get("caveats", []), f.get("exemption")] if line]
        help_ = find_help(p, POLICY)["needed"]
        if (route and route["measures"]) or extras or help_:
            with st.expander(t("see_work")):
                if route and route["measures"]:
                    st.dataframe(
                        [
                            {
                                "Work": m["measure"],
                                "Cost": m["cost"],
                                "Rating after": f"{m['rating_after']} ({m['band_after']})",
                            }
                            for m in route["measures"]
                        ],
                        hide_index=True,
                        width="stretch",
                    )
                for line in extras:
                    st.caption(line)
                if help_:
                    # "Who to contact": official registers only, and a draft to copy.
                    st.markdown(f"**{t('who_to_contact')}**")
                    html(view.help_list(help_))
                    choice = st.selectbox(
                        t("draft_for"),
                        [None, *help_],
                        format_func=lambda h: "—" if h is None else h["who"].capitalize(),
                        key=f"draft_{p.property_id}",
                    )
                    if choice is not None:
                        d = draft_quote_request(p, POLICY, choice["trade"])
                        st.caption(t("draft_note"))
                        draft_box(d, f"form_{p.property_id}_{choice['trade']}")

    st.caption(t("fix_early_note"))
    with st.expander(t("as_table")):  # the accessible table view of the cards above
        st.dataframe(table_rows(toolbox), width="stretch", hide_index=True)
    report = render(
        list(toolbox.props.values()),
        POLICY,
        "Your rental homes — route to EPC C",
        date.today(),
        problems,
    )
    st.download_button(t("report"), report, "route_to_c.html", "text/html")

    # --- Chat ------------------------------------------------------------------------------
    html(view.section(t("chat")))
    if not (secret("NEBIUS_API_KEY") and secret("NEBIUS_MODEL")):
        st.info(t("chat_off"))
    else:
        if "bot" not in ss:
            ss.bot = Assistant(toolbox)
        for i, (role, content, calls) in enumerate(ss.get("chat", [])):
            with st.chat_message(role):
                st.markdown(content)
                for j, c in enumerate(calls):  # drafts come straight from the tool, not the model
                    if c["name"] == "draft_quote_request" and "body" in c["result"]:
                        draft_box(c["result"], f"form_chat_{i}_{j}")
                    if c["name"] == "make_plan" and c["result"].get("homes"):
                        plan_view(c["result"], f"plan_{i}_{j}")
                if calls:
                    with st.expander(t("working")):
                        for c in calls:
                            st.code(f"{c['name']}({c['args'] or ''})", language=None)
        chat = ss.get("chat", [])
        over_limit = DEMO and sum(r == "user" for r, _, _ in chat) >= DEMO_QUESTIONS_PER_VISITOR
        if chat and chat[-1][0] == "assistant":  # next steps, picked by code from the tools used
            steps = [
                f
                for f in follow_ups(chat[-1][2], toolbox)
                if f["kind"] == "draft" or not over_limit
            ]
            if steps:
                st.caption(t("next_steps"))
                cols = st.columns(len(steps))
                for k, (col, f) in enumerate(zip(cols, steps, strict=True)):
                    label = t(f["label_key"], home=f.get("home", ""), who=f.get("who", ""))
                    if col.button(label, key=f"next_{len(chat)}_{k}", width="stretch"):
                        if f["kind"] == "draft":
                            offer_draft(f["home"], f["trade"], f["who"])
                        else:
                            ask(label)
        asked = sum(role == "user" for role, _, _ in ss.get("chat", []))
        if DEMO and asked >= DEMO_QUESTIONS_PER_VISITOR:
            st.info(t("chat_limit", n=DEMO_QUESTIONS_PER_VISITOR))
        else:
            if not ss.get("chat"):  # suggestions until the first question
                cols = st.columns(3)
                for col, key in zip(cols, ("suggest_1", "suggest_2", "suggest_3"), strict=True):
                    if col.button(t(key), key=f"btn_{key}", width="stretch"):
                        ask(t(key))
            if q := st.chat_input(t("chat_placeholder"), max_chars=MAX_QUESTION_CHARS):
                ask(q)

html(view.small_print(DISCLAIMER, LICENCE))
