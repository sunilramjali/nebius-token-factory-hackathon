"""Streamlit demo: landlord enters homes -> table -> chat with the assistant.

    uv run --env-file .env streamlit run app.py

Numbers come from the engine (local snapshot, data/epc.duckdb); the assistant only explains
them. Register addresses are shown only to settle an ambiguous match and never reach Nebius.
"""

from __future__ import annotations

import os
from datetime import date

import duckdb
import streamlit as st
from dotenv import load_dotenv

from agent.assistant import Assistant
from agent.tools import property_facts
from engine.policy import load_policy
from report.portfolio_report import DISCLAIMER, LICENCE, render
from webapp.logic import (
    build_toolbox,
    demo_entries,
    needs_input_message,
    resolve_rows,
    rows_from_entries,
    table_rows,
    text,
)

load_dotenv()
st.set_page_config(page_title="Route to EPC C", layout="wide")
POLICY = load_policy()
EMPTY_ROWS = [{"label": "", "address": "", "postcode": "", "uprn": ""} for _ in range(3)]


@st.cache_resource
def connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(os.environ.get("DUCKDB_TARGET", "data/epc.duckdb"), read_only=True)


def con() -> duckdb.DuckDBPyConnection:
    return connection().cursor()  # one cursor per use: Streamlit runs scripts in threads


def check(entries: list[dict]) -> None:
    rows = rows_from_entries(entries)
    if not rows:
        st.session_state.pop("toolbox", None)
        st.warning(text("no_homes", POLICY))
        return
    resolved = resolve_rows(con(), rows)
    st.session_state.resolved = resolved
    toolbox = build_toolbox(con(), resolved, POLICY)
    for r in resolved:  # found on the register but no engine result (as report_from_csv does)
        if r.uprn is not None and str(r.uprn) in toolbox.missing:
            r.status = "no_data"
    st.session_state.toolbox = toolbox
    st.session_state.chat = []
    st.session_state.pop("bot", None)


ss = st.session_state
ss.setdefault("entries", EMPTY_ROWS)
ss.setdefault("editor_key", 0)

st.title(text("title", POLICY))
st.caption(f"⚠️ {DISCLAIMER}")
st.write(text("intro", POLICY))

# --- 1. Enter homes ------------------------------------------------------------------------
st.caption(text("entry_help", POLICY))
edited = st.data_editor(
    ss.entries,
    num_rows="dynamic",
    width="stretch",
    key=f"editor_{ss.editor_key}",
    column_config={
        "label": st.column_config.TextColumn("Label (optional)"),
        "address": st.column_config.TextColumn("Address", width="large"),
        "postcode": st.column_config.TextColumn("Postcode"),
        "uprn": st.column_config.TextColumn("UPRN (optional)"),
    },
)
c1, c2, _ = st.columns([1, 1, 4])
if c1.button(text("check", POLICY), type="primary"):
    ss.entries = edited
    check(edited)
if c2.button(text("demo", POLICY)):
    ss.entries = demo_entries(con())
    ss.editor_key += 1  # a new key makes the editor show the new rows
    check(ss.entries)
    st.rerun()

# --- 2. Rows that need input -----------------------------------------------------------------
resolved = ss.get("resolved", [])
toolbox = ss.get("toolbox")
problems = [r for r in resolved if r.status not in ("matched", "given")]
if problems:
    st.subheader(text("needs_input", POLICY))
    for r in problems:
        r_label = f"{r.row.label} ({r.row.address})" if r.row.address else r.row.label
        st.markdown(f"**{r_label}**: {needs_input_message(r.status)}")
        if r.status == "ambiguous" and 1 < len(r.options) <= 8:
            # Register addresses, shown only to the landlord who typed this home.
            choice = st.selectbox(
                text("pick", POLICY),
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

# --- 3. Results ------------------------------------------------------------------------------
if toolbox and toolbox.props:
    st.subheader(text("results", POLICY))
    st.dataframe(table_rows(toolbox), width="stretch", hide_index=True)
    st.caption(text("fix_early_note", POLICY))

    for p in toolbox.props.values():
        f = property_facts(p, POLICY)
        with st.expander(f"{f['label']}: {f['status']}"):
            st.write(f["next_action"])
            route = f.get("cheapest_route")
            if route and route["measures"]:
                st.table(
                    [
                        {
                            "Measure": m["measure"],
                            "Rating gain": m["rating_gain"],
                            "Cost": m["cost"],
                            "Rating after": f"{m['band_after']} ({m['rating_after']})",
                        }
                        for m in route["measures"]
                    ]
                )
            for line in [*f.get("caveats", []), f.get("exemption")]:
                if line:
                    st.caption(line)

    html = render(
        list(toolbox.props.values()),
        POLICY,
        "Your rental portfolio — route to EPC C",
        date.today(),
        problems,
    )
    st.download_button(text("report", POLICY), html, "route_to_c.html", "text/html")

    # --- 4. Chat -----------------------------------------------------------------------------
    st.subheader(text("chat", POLICY))
    if not (os.environ.get("NEBIUS_API_KEY") and os.environ.get("NEBIUS_MODEL")):
        st.info(text("chat_off", POLICY))
    else:
        if "bot" not in ss:
            ss.bot = Assistant(toolbox)
        for role, content, calls in ss.get("chat", []):
            with st.chat_message(role):
                st.markdown(content)
                if calls:
                    with st.expander(text("working", POLICY)):
                        for c in calls:
                            st.code(f"{c['name']}({c['args'] or ''})", language=None)
        if q := st.chat_input(text("chat_placeholder", POLICY)):
            ss.chat.append(("user", q, []))
            with st.spinner("…"):
                try:
                    reply = ss.bot.ask(q)
                    ss.chat.append(("assistant", reply.text, reply.tool_calls))
                except Exception:  # network, rate limit: the table still works
                    ss.chat.append(("assistant", text("chat_error", POLICY), []))
            st.rerun()

st.divider()
st.caption(LICENCE)
