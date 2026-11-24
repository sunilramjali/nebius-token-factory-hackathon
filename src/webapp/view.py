"""HTML pieces for app.py: Apple-like styling and the data-viz components.

Forms (dataviz skill, choosing-a-form): headline numbers are stat tiles, and each home is a
meter (rating against the C line). Status colours are the skill's validated status palette
and always come with an icon and a label, never colour alone. Text stays in ink colours.

Every landlord-typed string goes through html.escape: labels are user input.
"""

from __future__ import annotations

from html import escape

from agent.tools import Toolbox, portfolio_summary
from report.portfolio_report import Property, cost_range, next_actions, status

# Status palette (dataviz references/palette.md) + a neutral for "survey first".
GOOD, WARNING, CRITICAL, NEUTRAL = "#0ca30c", "#fab219", "#d03b3b", "#8e8e93"
STATUS_STYLE = {  # status key -> (colour, icon)
    "done": (GOOD, "✓"),
    "fix": (GOOD, "✓"),
    "borderline": (WARNING, "!"),
    "quotes": (WARNING, "!"),
    "boiler": (WARNING, "!"),
    "over_cap": (CRITICAL, "×"),
    "cannot": (CRITICAL, "×"),
    "survey": (NEUTRAL, "?"),
}
RATING_MAX = 100  # EPC ratings run 1–100

CSS = """
<style>
:root {
  --ink: #1d1d1f; --ink-2: #6e6e73; --ink-3: #86868b;
  --surface: #ffffff; --card: #f5f5f7; --hairline: rgba(0,0,0,0.08);
  --accent: #0071e3; --track: #e8e8ed;
}
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
    "Helvetica Neue", Helvetica, Arial, sans-serif !important;
  -webkit-font-smoothing: antialiased;
}
.stApp { background: var(--surface); color: var(--ink); }
/* Streamlit's own font rules beat inheritance, so name the system font on our pieces. */
.hero, .hero *, .section-title, .pitch, .pitch *, .tiles, .tiles *, .home, .home *,
.small-print, [data-testid="stChatMessage"] p, [data-testid="stCaptionContainer"] {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
    "Helvetica Neue", Helvetica, Arial, sans-serif !important;
}
#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }
.block-container { max-width: 980px; padding-top: 2.5rem; padding-bottom: 4rem; }

.hero { text-align: center; padding: 2.5rem 0 1.5rem; }
.hero h1 { font-size: clamp(2.4rem, 6vw, 3.6rem); font-weight: 600; letter-spacing: -0.02em;
  line-height: 1.05; margin: 0; color: var(--ink); }
.hero p { font-size: clamp(1.1rem, 2.4vw, 1.45rem); color: var(--ink-2); margin: 0.9rem auto 0;
  max-width: 680px; line-height: 1.35; text-wrap: balance; }
.eyebrow { font-size: 0.85rem; color: var(--ink-3); text-align: center; margin-top: 0.6rem; }

.section-title { font-size: 1.9rem; font-weight: 600; letter-spacing: -0.015em;
  margin: 3rem 0 1rem; color: var(--ink); }

.pitch { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 1.5rem 0; }
.pitch div { background: var(--card); border-radius: 18px; padding: 20px 22px; }
.pitch b { display: block; font-size: 1.05rem; margin-bottom: 6px; color: var(--ink); }
.pitch span { color: var(--ink-2); font-size: 0.97rem; line-height: 1.4; }

.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 1rem 0; }
.tile { background: var(--card); border-radius: 18px; padding: 18px 20px; }
.tile .label { font-size: 0.9rem; color: var(--ink-2); }
.tile .value { font-size: 2rem; font-weight: 600; letter-spacing: -0.02em; color: var(--ink);
  margin-top: 4px; line-height: 1.1; }
.tile .value.small { font-size: 1.35rem; }
.tile .note { font-size: 0.8rem; color: var(--ink-3); margin-top: 4px; }

.home { background: var(--card); border-radius: 18px; padding: 18px 22px; margin-bottom: 4px; }
.home .top { display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
  flex-wrap: wrap; }
.home .name { font-size: 1.2rem; font-weight: 600; color: var(--ink); }
.home .status { font-size: 0.95rem; color: var(--ink); white-space: nowrap; }
.home .dot { display: inline-flex; width: 20px; height: 20px; border-radius: 50%;
  align-items: center; justify-content: center; color: #fff; font-size: 0.75rem;
  font-weight: 700; margin-right: 6px; vertical-align: 1px; }
.home .dot.warn { color: #0b0b0b; }
.meter { position: relative; height: 8px; border-radius: 4px; background: var(--track);
  margin: 30px 0 8px; }
.meter .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 4px; }
.meter .c-line { position: absolute; top: -5px; bottom: -5px; width: 2px;
  background: var(--ink); border-radius: 1px; }
.meter .c-tag { position: absolute; top: -22px; transform: translateX(-50%);
  font-size: 0.72rem; color: var(--ink-2); font-weight: 600; }
.home .facts { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  font-size: 0.92rem; color: var(--ink-2); }
.home .cost { color: var(--ink); font-weight: 600; }
.home .next { margin-top: 10px; font-size: 0.97rem; color: var(--ink); }

.small-print { font-size: 0.78rem; color: var(--ink-3); line-height: 1.45; margin-top: 3rem; }

.stButton button { border-radius: 980px; padding: 0.45rem 1.1rem; border: 1px solid var(--hairline);
  background: var(--card); color: var(--ink); font-weight: 500; }
.stButton button:hover { border-color: var(--accent); color: var(--accent); }
.stButton button[kind="primary"] { background: var(--accent); color: #fff; border: none; }
.stButton button[kind="primary"]:hover { background: #0077ed; color: #fff; }
/* "See the work": a quiet blue link under its card, not a boxed panel. */
[data-testid="stExpander"] { border: none; background: transparent; margin: 0 0 14px; }
[data-testid="stExpander"] details { border: none !important; background: transparent; }
[data-testid="stExpander"] summary { padding: 4px 22px; color: var(--accent); font-size: 0.92rem; }
[data-testid="stExpander"] summary:hover { color: #0077ed; }
[data-testid="stChatMessage"] { background: var(--card); border-radius: 18px; }

@media (max-width: 720px) {
  .pitch { grid-template-columns: 1fr; }
  .tiles { grid-template-columns: repeat(2, 1fr); }
}
</style>
"""


def hero(title: str, subtitle: str, eyebrow: str | None = None) -> str:
    extra = f"<div class='eyebrow'>{escape(eyebrow)}</div>" if eyebrow else ""
    return f"<div class='hero'><h1>{escape(title)}</h1><p>{escape(subtitle)}</p>{extra}</div>"


def section(title: str) -> str:
    return f"<div class='section-title'>{escape(title)}</div>"


def pitch(cards: list[tuple[str, str]]) -> str:
    inner = "".join(f"<div><b>{escape(h)}</b><span>{escape(t)}</span></div>" for h, t in cards)
    return f"<div class='pitch'>{inner}</div>"


def tiles(toolbox: Toolbox, labels: dict[str, str]) -> str:
    """KPI row: four stat tiles from the same summary the assistant sees."""
    s = portfolio_summary(list(toolbox.props.values()), toolbox.policy)
    total = len(s["homes"])
    spend = s["estimated_total_spend"] or "—"
    items = [
        (labels["below"], f"{s['homes_below_c']} of {total}", "", ""),
        (labels["fix_early"], str(s["fix_early"]), "", ""),
        (labels["over_cap"], str(s["likely_over_cap"]), "", ""),
        (labels["spend"], spend, "small", labels["spend_note"]),
    ]
    out = "".join(
        f"<div class='tile'><div class='label'>{escape(lab)}</div>"
        f"<div class='value {cls}'>{escape(val)}</div>"
        + (f"<div class='note'>{escape(note)}</div>" if note else "")
        + "</div>"
        for lab, val, cls, note in items
    )
    return f"<div class='tiles'>{out}</div>"


def short_of_c(p: Property, threshold: int) -> str:
    gap = threshold - p.current_score
    if gap <= 0:
        return f"Rating {p.current_score} · C or better"
    return f"Rating {p.current_score} · {gap} short of C"


def home_card(p: Property, toolbox: Toolbox) -> str:
    """One home: name, status (icon + label), meter against the C line, cost, next step."""
    policy = toolbox.policy
    st = status(p, policy)
    colour, icon = STATUS_STYLE.get(st.key, (NEUTRAL, "?"))
    warn = " warn" if colour == WARNING else ""  # dark icon on yellow (palette label rule)
    width = max(0, min(100, p.current_score / RATING_MAX * 100))
    c_at = policy.band_c_threshold / RATING_MAX * 100
    cost = (
        f"About {cost_range(p.cost_low, p.cost_high)}"
        if st.key not in {"done", "survey", "cannot"} and p.cost_known
        else ""
    )
    tip = (
        f"{p.label}: rating {p.current_score} ({p.current_band}). "
        f"C starts at {policy.band_c_threshold}."
    )
    return (
        f"<div class='home'><div class='top'><span class='name'>{escape(p.label)}</span>"
        f"<span class='status'><span class='dot{warn}' style='background:{colour}'>{icon}</span>"
        f"{escape(st.label)}</span></div>"
        f"<div class='meter' title='{escape(tip, quote=True)}' role='img' "
        f"aria-label='{escape(tip, quote=True)}'>"
        f"<div class='fill' style='width:{width:.1f}%;background:{colour}'></div>"
        f"<div class='c-line' style='left:{c_at:.1f}%'></div>"
        f"<div class='c-tag' style='left:{c_at:.1f}%'>C</div></div>"
        f"<div class='facts'><span>{escape(short_of_c(p, policy.band_c_threshold))}</span>"
        f"<span class='cost'>{escape(cost)}</span></div>"
        f"<div class='next'>{escape(next_actions([p], policy)[0])}</div></div>"
    )


def small_print(*lines: str) -> str:
    return "<div class='small-print'>" + "<br>".join(escape(x) for x in lines) + "</div>"
