"""Sleeper Cell — Streamlit presentation layer.

THIN. All business logic lives in src/. This file is allowed to import streamlit;
no other module in this repo is.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Then enter your Sleeper League ID and Draft ID in the sidebar. Both are visible in the
Sleeper URL when you're inside the league or the draft room. Read-only — no auth.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from src.data_layer import DataLayer
from src.draft_state import DraftState
from src.dynasty_curve import dynasty_value_breakdown
from src.future_pick_value import build_pick_value_model
from src.league_config import LeagueConfig
from src.player_index import PlayerIndex
from src.trade_analysis import (
    PlayerAsset,
    PickAsset,
    build_offer_message,
    evaluate_trade,
    pick_asset,
    player_asset,
)
from src.trade_recommender import SEARCH_DEPTHS, plan_acquisition, recommend_offers
from src.transaction_history import (
    activity_per_roster,
    grade_trades,
    parse_trades,
    to_graded_rows,
    to_normalized_rows,
)
from src.projections import (
    build_projection_index,
    explain_scoring_components,
    points_per_game,
)
from src.ktc_loader import FantasyCalcResult, KTCLoadResult, fetch_fantasycalc, load_ktc
from src.normalize import fmt_ba
from src.simulator import build_consensus_index, simulate_next_picks
from src.sleeper_client import SleeperClient, SleeperError
from src.team_analysis import league_needs, positional_scarcity, team_summary
from src.tier_engine import detect_tiers, tier_cliff_alerts
from src.valuation import filter_undrafted, value_population
from src.vbd import VBDContext, best_available_dynamic, likely_next_available_value


# --------------------------------------------------------------- Setup

st.set_page_config(page_title="Sleeper Cell", layout="wide", initial_sidebar_state="expanded")


# ---------------------------------------------------------------- Password gate
# If `auth.password` is set in Streamlit secrets, gate the entire app behind it.
# If not set (local dev), the app opens normally. Session-scoped — one entry per browser.
def _require_password() -> bool:
    expected = None
    if hasattr(st, "secrets"):
        try:
            expected = st.secrets.get("auth", {}).get("password")
        except Exception:
            expected = None
    if not expected:
        return True   # no gate configured
    if st.session_state.get("_pw_ok"):
        return True
    st.markdown("<h1>Sleeper Cell</h1>", unsafe_allow_html=True)
    st.caption("Private — enter the access password.")
    pw = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="password")
    if pw and pw == expected:
        st.session_state["_pw_ok"] = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    return False


if not _require_password():
    st.stop()

# ---------------------------------------------------------------- Theme selector
# Robinhood-inspired: clean, soft, breathable. Light = day mode (white canvas, slate ink,
# sage green for gains). Dark = night mode (near-black canvas, off-white ink, neon green
# for gains). Toggle lives in the sidebar; persisted in session_state.
# We inject the base stylesheet ALWAYS, then layer a theme-specific override that
# rewrites the CSS variables. Streamlit reruns re-inject both blocks — instant flip.
if "theme" not in st.session_state:
    st.session_state["theme"] = "Light"

# ---------------------------------------------------------------- Global stylesheet
# Modern dashboard feel: subtle elevation (shadows over borders), strong type hierarchy,
# generous whitespace between sections, color used ONLY for semantic meaning.
st.markdown("""
<style>
:root {
  /* LIGHT (default) — Robinhood-Day palette */
  --bg-page:     #ffffff;
  --bg-soft:     #f7f9fc;
  --bg-card:     #ffffff;
  --bg-sidebar:  #fafbfd;
  --bg-elevated: #ffffff;
  --border:      #e6eaf0;
  --border-soft: #eef1f6;
  --ring:        rgba(15, 23, 42, 0.06);
  --shadow-sm:   0 1px 2px 0 rgba(15, 23, 42, 0.04);
  --shadow-md:   0 4px 14px -4px rgba(15, 23, 42, 0.08), 0 2px 6px -3px rgba(15, 23, 42, 0.04);
  --shadow-lg:   0 12px 32px -8px rgba(15, 23, 42, 0.12), 0 4px 12px -4px rgba(15, 23, 42, 0.06);
  --ink:         #0c1320;
  --ink-2:       #2b3445;
  --ink-3:       #475467;
  --muted:       #667085;
  --muted-2:     #98a2b3;
  --muted-3:     #d0d5dd;
  --accent:      #0c1320;
  --good:        #15803d;
  --good-bright: #16a34a;
  --good-soft:   #e6f5ec;
  --warn:        #b45309;
  --warn-soft:   #fff4e0;
  --bad:         #b91c1c;
  --bad-soft:    #fdecec;
}

/* Reset Streamlit chrome */
.block-container {
  padding: 1.5rem 2rem 3rem !important;
  max-width: 1500px;
}
[data-testid="stHeader"] { background: transparent; height: 0; }
[data-testid="stToolbar"] { right: 0.75rem; top: 0.5rem; }
footer, [data-testid="stStatusWidget"] { display: none !important; }

/* Typography */
html, body, [class*="css"] {
  font-family: "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-feature-settings: "cv11" 1, "ss01" 1, "tnum" 1;
  -webkit-font-smoothing: antialiased;
  color: var(--ink);
}
h1 {
  font-size: 1.65rem !important;
  font-weight: 700 !important;
  letter-spacing: -0.02em;
  line-height: 1.15;
  margin: 0 0 0.2rem 0 !important;
  color: var(--ink);
}
h2 {
  font-size: 1.05rem !important;
  font-weight: 650 !important;
  letter-spacing: -0.01em;
  margin: 1.5rem 0 0.5rem 0 !important;
  color: var(--ink);
}
h3, .stMarkdown h3 {
  font-size: 0.7rem !important;
  font-weight: 700 !important;
  color: var(--muted) !important;
  text-transform: uppercase !important;
  letter-spacing: 0.09em !important;
  margin: 1rem 0 0.5rem 0 !important;
}
.stMarkdown h4, h4 {
  font-size: 0.88rem !important;
  font-weight: 600 !important;
  color: var(--ink) !important;
  margin: 0.75rem 0 0.3rem 0 !important;
}
.stMarkdown h5, h5 {
  font-size: 0.72rem !important;
  font-weight: 600 !important;
  color: var(--ink-3) !important;
  text-transform: uppercase !important;
  letter-spacing: 0.08em !important;
  margin: 1rem 0 0.4rem 0 !important;
}

/* Tabs — bolder selected state, subtle hover */
.stTabs [data-baseweb="tab-list"] {
  gap: 0.25rem !important;
  border-bottom: 1px solid var(--border) !important;
  margin-bottom: 1.5rem !important;
  padding-bottom: 0 !important;
}
.stTabs [data-baseweb="tab"] {
  padding: 0.55rem 1rem !important;
  font-size: 0.83rem !important;
  font-weight: 550 !important;
  color: var(--muted) !important;
  border-radius: 6px 6px 0 0;
  transition: color 120ms ease, background 120ms ease;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--ink-2) !important; background: var(--bg-soft) !important; }
.stTabs [aria-selected="true"] {
  color: var(--ink) !important;
  border-bottom: 2px solid var(--accent) !important;
  background: transparent !important;
}
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }

/* Sidebar — cleaner, more breathable */
[data-testid="stSidebar"] {
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
}
[data-testid="stSidebar"] > div:first-child { padding-top: 1rem; }
[data-testid="stSidebar"] .stMarkdown h2 {
  font-size: 0.68rem !important;
  color: var(--muted-2) !important;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin: 1.25rem 0 0.5rem 0 !important;
}
[data-testid="stSidebar"] label p { font-size: 0.82rem; color: var(--ink-3); }
[data-testid="stSidebar"] .stRadio label { font-size: 0.78rem !important; }
[data-testid="stSidebar"] [data-testid="stTextInput"] input,
[data-testid="stSidebar"] [data-testid="stNumberInput"] input { font-size: 0.85rem; }

/* Metric tiles — subtle elevation, no harsh border */
[data-testid="stMetric"] {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  padding: 0.75rem 0.95rem;
  box-shadow: var(--shadow-sm);
  transition: box-shadow 150ms ease;
}
[data-testid="stMetric"]:hover { box-shadow: var(--shadow-md); }
[data-testid="stMetricLabel"] {
  font-size: 0.66rem !important;
  color: var(--muted) !important;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  font-weight: 600;
}
[data-testid="stMetricValue"] {
  font-size: 1.4rem !important;
  font-weight: 700 !important;
  color: var(--ink) !important;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
  line-height: 1.2;
}
[data-testid="stMetricDelta"] { font-size: 0.7rem !important; opacity: 0.8; }

/* DataFrames — soft elevation, cleaner rows */
[data-testid="stDataFrame"] {
  border: 1px solid var(--border-soft) !important;
  border-radius: 8px !important;
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

/* Inputs — soft borders, subtle focus ring */
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-baseweb="select"] > div {
  border-radius: 6px !important;
  border-color: var(--border) !important;
  font-size: 0.85rem;
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
[data-testid="stTextInput"] input:focus,
[data-testid="stNumberInput"] input:focus {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px var(--ring) !important;
}

/* Buttons — match the dashboard feel */
[data-testid="baseButton-secondary"], .stButton button {
  background: var(--bg-card) !important;
  border: 1px solid var(--border) !important;
  border-radius: 6px !important;
  font-size: 0.82rem !important;
  font-weight: 550 !important;
  color: var(--ink-2) !important;
  padding: 0.4rem 0.85rem !important;
  box-shadow: var(--shadow-sm);
  transition: background 120ms ease, box-shadow 120ms ease;
}
.stButton button:hover { background: var(--bg-soft) !important; box-shadow: var(--shadow-md); }
.stButton button:active { transform: translateY(1px); }

/* Captions */
[data-testid="stCaptionContainer"] p, .caption {
  color: var(--muted) !important;
  font-size: 0.74rem !important;
  font-style: normal !important;
  line-height: 1.5;
  margin: 0.15rem 0;
}

/* Code blocks — modern monospace */
pre, code { font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace !important; }
pre code { font-size: 0.78rem !important; }

/* Dividers — softer, more breathing room */
hr {
  margin: 1.5rem 0 !important;
  border: none !important;
  border-top: 1px solid var(--border-soft) !important;
}

/* Position tag — slightly more refined */
.tag {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 0.66rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  font-family: "Inter", ui-sans-serif, system-ui, sans-serif;
  vertical-align: middle;
  line-height: 1.3;
}
.tag-qb { background: #2563eb; color: white; }
.tag-rb { background: #059669; color: white; }
.tag-wr { background: #dc2626; color: white; }
.tag-te { background: #d97706; color: white; }
.tag-flat { background: var(--bg-soft); color: var(--ink-2); border: 1px solid var(--border); }

/* Section header — title + right-aligned meta on the same baseline */
.sec-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin: 1.75rem 0 0.5rem 0;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid var(--border-soft);
}
.sec-head h2 { margin: 0 !important; }
.sec-head .meta {
  color: var(--muted);
  font-size: 0.73rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

/* Card wrapper — opt-in subtle elevation for grouped content */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  padding: 1rem 1.1rem;
  margin-bottom: 1rem;
  box-shadow: var(--shadow-sm);
}

/* Verdict banner — semantic color, tasteful */
.verdict {
  border-left: 4px solid var(--accent);
  background: var(--bg-soft);
  padding: 0.75rem 1rem;
  margin: 0.5rem 0 1.25rem 0;
  border-radius: 0 6px 6px 0;
}
.verdict .label {
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
  font-weight: 700;
}
.verdict .text {
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--ink);
  margin-top: 3px;
  letter-spacing: -0.005em;
}
.verdict .sub {
  font-size: 0.76rem;
  color: var(--muted);
  margin-top: 6px;
  font-variant-numeric: tabular-nums;
}
.verdict.send   { border-left-color: var(--good); background: var(--good-soft); }
.verdict.send  .text { color: var(--good); }
.verdict.offer  { border-left-color: var(--warn); background: var(--warn-soft); }
.verdict.offer .text { color: var(--warn); }
.verdict.pass   { border-left-color: var(--bad); background: var(--bad-soft); }
.verdict.pass  .text { color: var(--bad); }

/* Status pill — for stance badges, etc. */
.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 0.65rem;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  background: var(--bg-soft);
  color: var(--ink-3);
  border: 1px solid var(--border-soft);
}
.pill-good { background: var(--good-soft); color: #047857; border-color: transparent; }
.pill-warn { background: var(--warn-soft); color: #b45309; border-color: transparent; }
.pill-bad  { background: var(--bad-soft);  color: #b91c1c; border-color: transparent; }

/* Plain "label : value" inline rows */
.kv {
  font-size: 0.78rem;
  color: var(--ink-3);
  margin: 0.15rem 0;
  line-height: 1.5;
}
.kv span.k {
  color: var(--muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 0.62rem;
  margin-right: 10px;
}

/* Expander — softer */
.streamlit-expanderHeader, [data-testid="stExpander"] summary {
  font-size: 0.84rem !important;
  font-weight: 550 !important;
  color: var(--ink-2) !important;
}

/* Anchor links inline */
a { color: var(--ink); text-decoration: underline; text-decoration-color: var(--muted-3); text-underline-offset: 2px; }
a:hover { text-decoration-color: var(--ink-2); }

/* ---- Robinhood-style row card (used by Ranks drill-down) ------------------ */
.rh-row {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  padding: 0.7rem 1rem;
  margin-bottom: 0.45rem;
  box-shadow: var(--shadow-sm);
  transition: box-shadow 140ms ease, transform 140ms ease, border-color 140ms ease;
}
.rh-row:hover { box-shadow: var(--shadow-md); border-color: var(--border); }
.rh-row .name {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--ink);
  letter-spacing: -0.005em;
}
.rh-row .meta {
  font-size: 0.72rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.rh-row .ba {
  font-variant-numeric: tabular-nums;
  font-weight: 650;
  font-size: 0.92rem;
  color: var(--ink);
  letter-spacing: -0.005em;
}
.rh-row .delta-up   { color: var(--good); font-weight: 600; }
.rh-row .delta-down { color: var(--bad);  font-weight: 600; }

/* ---- Stat grid (used in player drill-down) ------------------------------ */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.6rem; margin: 0.7rem 0; }
.stat-cell {
  background: var(--bg-soft);
  border-radius: 8px;
  padding: 0.55rem 0.75rem;
}
.stat-cell .k { font-size: 0.62rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.09em; font-weight: 600; }
.stat-cell .v { font-size: 1.05rem; color: var(--ink); font-weight: 650; font-variant-numeric: tabular-nums; margin-top: 2px; }

/* ---- Theme-switch chip ------------------------------------------------- */
.theme-hint { font-size: 0.7rem; color: var(--muted); margin: 0.2rem 0 0.6rem 0; }
</style>
""", unsafe_allow_html=True)

# ---- DARK THEME OVERRIDES (Robinhood-Night) ------------------------------
# Layered ON TOP of the base sheet. Only re-declares the CSS variables —
# every component that uses var(--*) automatically picks up the new values.
if st.session_state.get("theme") == "Dark":
    st.markdown("""
    <style>
    :root {
      --bg-page:     #0b0e14;
      --bg-soft:     #11151d;
      --bg-card:     #141923;
      --bg-sidebar:  #0e121a;
      --bg-elevated: #1a2030;
      --border:      #232a38;
      --border-soft: #1b2230;
      --ring:        rgba(34, 197, 94, 0.18);
      --shadow-sm:   0 1px 2px 0 rgba(0, 0, 0, 0.4);
      --shadow-md:   0 4px 14px -4px rgba(0, 0, 0, 0.55), 0 2px 6px -3px rgba(0, 0, 0, 0.4);
      --shadow-lg:   0 12px 32px -8px rgba(0, 0, 0, 0.7);
      --ink:         #e8eef7;
      --ink-2:       #c3cad6;
      --ink-3:       #9aa3b2;
      --muted:       #76808f;
      --muted-2:     #5b6473;
      --muted-3:     #353c47;
      --accent:      #22c55e;
      --good:        #22c55e;
      --good-bright: #4ade80;
      --good-soft:   rgba(34, 197, 94, 0.14);
      --warn:        #f59e0b;
      --warn-soft:   rgba(245, 158, 11, 0.14);
      --bad:         #ef4444;
      --bad-soft:    rgba(239, 68, 68, 0.14);
    }
    /* Stronger anchor for the dark canvas so Streamlit's white toolbar/etc disappear */
    [data-testid="stAppViewContainer"], .main, .stApp { background: var(--bg-page) !important; }
    [data-testid="stSidebar"] { background: var(--bg-sidebar) !important; }
    /* Match dataframes to the dark canvas */
    [data-testid="stDataFrame"] { background: var(--bg-card) !important; }
    /* Active tab uses neon green underline for the Robinhood-Night feel */
    .stTabs [aria-selected="true"] { border-bottom-color: var(--accent) !important; color: var(--ink) !important; }
    /* Inputs need explicit dark backgrounds */
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-baseweb="select"] > div,
    [data-baseweb="input"] input {
      background: var(--bg-card) !important;
      color: var(--ink) !important;
    }
    .stButton button { background: var(--bg-card) !important; color: var(--ink-2) !important; }
    .stButton button:hover { background: var(--bg-elevated) !important; }
    pre, code { color: var(--ink-2) !important; }
    pre { background: var(--bg-soft) !important; border: 1px solid var(--border-soft) !important; }
    </style>
    """, unsafe_allow_html=True)

REPO_ROOT = Path(__file__).parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
SLEEPER_CACHE = DATA_DIR / "sleeper"
DP_CACHE = DATA_DIR / "dp"
SLEEPER_CACHE.mkdir(exist_ok=True)
DP_CACHE.mkdir(exist_ok=True)


def pos_chip(pos: str) -> str:
    """Position tag. Kept ascii — class names drive color."""
    cls = {"QB": "tag-qb", "RB": "tag-rb", "WR": "tag-wr", "TE": "tag-te"}.get(pos, "tag-flat")
    return f'<span class="tag {cls}">{pos}</span>'


# --------------------------------------------------------------- Cached data

@st.cache_resource(show_spinner="Connecting to Sleeper…")
def get_client() -> SleeperClient:
    return SleeperClient(cache_dir=SLEEPER_CACHE)


@st.cache_resource(show_spinner="Loading DynastyProcess data layer…")
def get_data_layer() -> DataLayer:
    return DataLayer(cache_dir=DP_CACHE)


@st.cache_data(ttl=21600, show_spinner="Caching Sleeper player catalog (one-time, ~5MB)…")
def cached_players(_client: SleeperClient) -> dict:
    return _client.get_players_nfl()


@st.cache_data(ttl=21600, show_spinner="Loading season projections (~8MB)…")
def cached_projections(_client: SleeperClient, season: str) -> list[dict]:
    return _client.get_season_projections(season)


@st.cache_data(ttl=3600, show_spinner="Loading league…")
def cached_league(_client: SleeperClient, league_id: str) -> dict | None:
    return _client.get_league(league_id)


@st.cache_data(ttl=3600)
def cached_users(_client: SleeperClient, league_id: str) -> list[dict]:
    return _client.get_users(league_id)


@st.cache_data(ttl=3600)
def cached_rosters(_client: SleeperClient, league_id: str) -> list[dict]:
    return _client.get_rosters(league_id)


@st.cache_data(ttl=3600)
def cached_league_traded_picks(_client: SleeperClient, league_id: str) -> list[dict]:
    return _client.get_league_traded_picks(league_id)


@st.cache_data(ttl=300)
def cached_transactions(_client: SleeperClient, league_id: str) -> list[dict]:
    """Pre-season + draft trades land in week 1 for most leagues. TTL low so new
    trades surface quickly during an active draft.

    Graceful failure: returns [] if the client lacks the method (stale cache_resource
    instance from before this code shipped) or if any other error occurs. Trade history
    is best-effort; the rest of the app should still render.
    """
    if not hasattr(_client, "get_all_transactions"):
        return []
    try:
        return _client.get_all_transactions(league_id) or []
    except Exception:
        return []


@st.cache_data(ttl=5)
def cached_draft(_client: SleeperClient, draft_id: str) -> dict | None:
    return _client.get_draft(draft_id)


@st.cache_data(ttl=5)
def cached_picks(_client: SleeperClient, draft_id: str) -> list[dict]:
    return _client.get_draft_picks(draft_id)


@st.cache_data(ttl=5)
def cached_draft_traded(_client: SleeperClient, draft_id: str) -> list[dict]:
    return _client.get_draft_traded_picks(draft_id)


@st.cache_resource(show_spinner="Building player index (DP cross-walk)…")
def cached_player_index(_players: dict, _dp: DataLayer) -> PlayerIndex:
    return PlayerIndex(sleeper_players=_players, dp=_dp).build()


# --------------------------------------------------------------- Sidebar

with st.sidebar:
    st.markdown("<h1 style='margin-bottom:0'>Sleeper Cell</h1>", unsafe_allow_html=True)
    st.caption("Live dynasty draft intelligence")

    # Theme toggle — at the very top so users see it before anything else.
    # The radio's `key=` binds it to session_state["theme"] directly, so flipping
    # the radio automatically reruns the script (Streamlit default) and the
    # theme-override CSS block downstream picks up the new value.
    st.radio(
        "Appearance",
        ["Light", "Dark"],
        horizontal=True,
        key="theme",
        help="Light = Robinhood-Day (white canvas, slate ink). Dark = Robinhood-Night (near-black, neon green accents).",
    )

    default_league = st.secrets.get("sleeper", {}).get("league_id") if hasattr(st, "secrets") else None
    default_draft = st.secrets.get("sleeper", {}).get("draft_id") if hasattr(st, "secrets") else None

    st.markdown("## Identifiers")
    league_id = st.text_input("League ID", value=default_league or "")
    draft_id = st.text_input("Draft ID", value=default_draft or "")
    default_user = st.secrets.get("sleeper", {}).get("user_id") if hasattr(st, "secrets") else None
    user_input = st.text_input(
        "Your username OR user ID",
        value=default_user or "",
        help=(
            "Either works:\n"
            "• Username — what you log in with (e.g. `tbanks44`)\n"
            "• User ID — the 18-digit number from your Sleeper profile\n\n"
            "Used to identify 'your' roster. Leave blank to default to the first roster."
        ),
        placeholder="e.g. tbanks44",
    )

    st.markdown("## Strategy")
    strategy = st.radio(
        "Strategy", ["compete", "balanced", "rebuild"], index=0, horizontal=True,
        label_visibility="collapsed",
        help="Compete weights near-term production. Rebuild discounts year 1.",
    )
    horizon = st.slider("Dynasty horizon (years)", min_value=2, max_value=8, value=4)

    st.markdown("## Bylaws")
    bylaw_3rr = st.checkbox("Third Round Reversal", value=True)

    st.markdown("## Community values overlay")
    fc_clicked = st.button(
        "Fetch latest from FantasyCalc",
        help="One-click. Pulls live SF dynasty values from api.fantasycalc.com. "
             "Direct Sleeper-ID join — no fuzzy matching, no CSV. Refreshes nightly.",
        width="stretch",
    )
    if fc_clicked:
        with st.spinner("Fetching FantasyCalc dynasty values..."):
            fc_result = fetch_fantasycalc(superflex=True, num_teams=12, ppr=1.0)
            st.session_state["_fc_result"] = fc_result
            if fc_result.error:
                st.error(f"FantasyCalc fetch failed: {fc_result.error}")
            else:
                st.success(f"Loaded {fc_result.rows_with_sleeper_id} player values from FantasyCalc.")

    with st.expander("Or upload KTC CSV"):
        ktc_file = st.file_uploader(
            "KTC CSV",
            type=["csv"],
            help="Paste KeepTradeCut SF dynasty rankings into a CSV "
                 "(Player,Value or Player,Pos,Value). Fuzzy-matched to Sleeper IDs.",
            label_visibility="collapsed",
        )

    st.markdown("## Refresh")
    auto = st.checkbox("Auto-refresh (8 s)", value=True)
    if auto:
        st_autorefresh(interval=8000, key="autorefresh_8s")

if not league_id or not draft_id:
    st.title("Sleeper Cell")
    st.info("Enter your Sleeper **League ID** and **Draft ID** in the sidebar to begin.\n\n"
            "League ID is in the URL when you're in the league:  `sleeper.com/leagues/<LEAGUE_ID>/...`\n\n"
            "Draft ID is in the URL when you're in the draft room:  `sleeper.com/draft/nfl/<DRAFT_ID>`")
    st.stop()


# --------------------------------------------------------------- Load everything

client = get_client()
dl = get_data_layer()

try:
    league_raw = cached_league(client, league_id)
    if not league_raw:
        st.error(f"League {league_id} not found. Check the ID and try again.")
        st.stop()

    draft_raw = cached_draft(client, draft_id)
    if not draft_raw:
        st.error(f"Draft {draft_id} not found.")
        st.stop()

    users = cached_users(client, league_id)
    rosters = cached_rosters(client, league_id)
    ltp = cached_league_traded_picks(client, league_id)
    picks = cached_picks(client, draft_id)
    dtp = cached_draft_traded(client, draft_id)
    transactions_raw = cached_transactions(client, league_id)

    players_catalog = cached_players(client)
    season = str(league_raw.get("season") or "2026")
    projections_raw = cached_projections(client, season)

except SleeperError as e:
    st.error(f"Sleeper API error: {e}")
    st.stop()

cfg = LeagueConfig.from_sleeper_league(
    league_raw,
    draft=draft_raw,
    bylaws={"third_round_reversal": bylaw_3rr},
    strategy=strategy,
)
cfg.age_horizon_years = horizon

state = DraftState(
    cfg=cfg,
    draft=draft_raw,
    picks=picks,
    draft_traded_picks=dtp,
    league_traded_picks=ltp,
).build()

projections = build_projection_index(projections_raw, cfg)
index = cached_player_index(players_catalog, dl)

# Community values overlay: FantasyCalc fetch (session-sticky) takes precedence;
# falls back to KTC CSV upload or data/ktc.csv file. Either produces sleeper_id → value.
ktc_values: dict[str, float] = {}
ktc_result: KTCLoadResult | None = None
fc_result_state: FantasyCalcResult | None = st.session_state.get("_fc_result")
overlay_source: str | None = None

if fc_result_state and fc_result_state.by_player_id:
    ktc_values = fc_result_state.by_player_id
    overlay_source = "FantasyCalc"
else:
    try:
        if ktc_file is not None:
            ktc_result = load_ktc(ktc_file, players_catalog)
        else:
            default_ktc_path = DATA_DIR / "ktc.csv"
            if default_ktc_path.exists():
                ktc_result = load_ktc(default_ktc_path, players_catalog)
        if ktc_result:
            ktc_values = ktc_result.by_player_id
            overlay_source = "KTC CSV"
    except Exception as e:
        st.sidebar.warning(f"KTC load failed: {e}")

valued_all = value_population(projections, index, cfg, ktc_values=ktc_values)
drafted_ids = state.drafted_ids()
valued_undrafted = filter_undrafted(valued_all, drafted_ids)

# Surface overlay status in the sidebar so the user can audit
with st.sidebar:
    if fc_result_state and fc_result_state.by_player_id:
        st.caption(
            f"Overlay: **FantasyCalc** · {fc_result_state.rows_with_sleeper_id} players matched "
            f"(of {fc_result_state.rows_fetched} fetched)"
        )
    elif ktc_result is not None and ktc_result.total_matched > 0:
        st.caption(
            f"Overlay: **KTC CSV** · {ktc_result.total_matched} matched "
            f"({len(ktc_result.unmatched)} unmatched of {ktc_result.rows_read} rows)"
        )
        if ktc_result.unmatched:
            with st.expander("Unmatched names"):
                st.write(", ".join(ktc_result.unmatched[:50]))
    elif ktc_result is not None and ktc_result.total_matched == 0:
        st.warning("KTC file loaded but 0 names matched. Check column headers.")

# Identity: resolve sidebar `user_input` to a roster_id. Accepts:
#   - numeric Sleeper user_id (e.g. "1032591150932180992")
#   - username (e.g. "tbanks44" or "TBanks44") — looked up via /v1/user/{username}
#   - blank → falls back to first roster in the league, with a sidebar warning
my_roster_id: int | None = None
my_user_id: str | None = None
if user_input:
    uin = user_input.strip()
    if uin.isdigit():
        my_user_id = uin
    else:
        # Try the literal input first, then lower-case (Sleeper usernames are case-insensitive
        # in some endpoints, sensitive in others — try both rather than guess).
        for candidate in (uin, uin.lower()):
            try:
                user_obj = client.get_user(candidate)
            except SleeperError:
                continue
            if user_obj and user_obj.get("user_id"):
                my_user_id = str(user_obj["user_id"])
                break

if my_user_id:
    for r in rosters:
        if str(r.get("owner_id")) == str(my_user_id):
            my_roster_id = int(r["roster_id"])
            break

if not my_roster_id and rosters:
    my_roster_id = int(rosters[0]["roster_id"])
    if user_input:
        with st.sidebar:
            st.warning(
                f"Couldn't find user '{user_input}' in this league. "
                f"Defaulting to first roster."
            )

# Lookup helpers
uid_to_name = {u["user_id"]: (u.get("display_name") or u["user_id"]) for u in users}
roster_to_uid = {r["roster_id"]: r["owner_id"] for r in rosters}
def roster_name(rid: int) -> str:
    uid = roster_to_uid.get(rid, "")
    return uid_to_name.get(uid, f"roster {rid}")


# --------------------------------------------------------------- Header

flags = []
if cfg.superflex: flags.append("SF")
if cfg.te_premium: flags.append(f"TEP +{cfg.scoring.get('bonus_rec_te',0):g}")
if cfg.best_ball: flags.append("BB")
if cfg.third_round_reversal: flags.append("3RR")
flags.append(f"{int(cfg.pass_td_value)}pt pass TD")
if cfg.completion_bonus: flags.append(f"+{cfg.completion_bonus}/cmp")
flags.append(f"{cfg.season} · {cfg.teams} teams")

st.markdown(f"<h1>{cfg.name}</h1>", unsafe_allow_html=True)
st.caption(" &middot; ".join(flags))

# KPI strip
kpi_cols = st.columns(4)
onc = state.on_clock()
my_next = state.upcoming_for_roster(my_roster_id) if my_roster_id else []
picks_until_mine = (my_next[0].pick_no - onc.pick_no) if (my_next and onc) else 0
with kpi_cols[0]:
    st.metric("Draft status", str(draft_raw.get("status", "?")).title())
with kpi_cols[1]:
    st.metric("Picks made", f"{len(picks)} / {len(state.schedule)}")
with kpi_cols[2]:
    st.metric("On the clock", roster_name(onc.owner_roster_id) if onc else "—")
with kpi_cols[3]:
    if my_next:
        st.metric("Your next pick", f"#{my_next[0].pick_no} (R{my_next[0].round})",
                  delta=f"{picks_until_mine} picks out", delta_color="off")
    else:
        st.metric("Your next pick", "—")


# --------------------------------------------------------------- Tabs

# Robinhood-style IA: Home (draft snapshot) · Ranks (every player + drill-down) ·
# Trades (acquire / find / build / history) · League (your team + partners) · Why? (transparency)
tab_home, tab_ranks, tab_trades, tab_league, tab_explain = st.tabs([
    "Home", "Ranks", "Trades", "League", "Why?",
])
# Backwards-compat aliases so the existing section bodies keep working unchanged
tab_board = tab_home
tab_best = tab_ranks
tab_tiers = tab_ranks   # Tier alerts now live inside the Ranks tab
tab_team = tab_league   # My team folds into the League tab
tab_trade = tab_trades


# ------------------------------ Tab: Draft Board ------------------------------

with tab_board:
    # ----- Home hero: your team at a glance (Robinhood-style "portfolio" header) -----
    made_n = len([sp for sp in state.schedule if sp.made])
    total_n = len(state.schedule)
    my_upcoming = [sp for sp in state.schedule if not sp.made and sp.owner_roster_id == my_roster_id]
    next_pick = my_upcoming[0] if my_upcoming else None
    on_clock = next((sp for sp in state.schedule if not sp.made), None)
    on_clock_label = (
        f"R{on_clock.round}.{on_clock.slot_pos} · {roster_name(on_clock.owner_roster_id)}"
        if on_clock else "—"
    )
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Draft progress", f"{made_n}/{total_n}", help="Picks made vs. total picks across all rounds.")
    h2.metric(
        "On the clock",
        on_clock_label,
        help="The current pick — who picks next and at what slot.",
    )
    h3.metric(
        "Your next pick",
        f"R{next_pick.round}.{next_pick.slot_pos}" if next_pick else "—",
        delta=f"#{next_pick.pick_no} overall" if next_pick else None,
        help="Your next upcoming startup pick (pick number and round.slot).",
    )
    h4.metric(
        "Picks remaining (you)",
        len(my_upcoming),
        help="How many of your own startup picks are still unmade.",
    )

    # ----- Recent picks (clean table) -----
    st.markdown('<div class="sec-head"><h2>Recent picks</h2>'
                f'<div class="meta">{made_n} of {total_n} made</div></div>',
                unsafe_allow_html=True)
    recent = sorted([sp for sp in state.schedule if sp.made], key=lambda x: -x.pick_no)[:18]
    if recent:
        recent_rows = []
        for sp in recent:
            proj = projections.get(sp.player_id or "")
            recent_rows.append({
                "Pick": sp.pick_no,
                "Round": f"R{sp.round}.{sp.slot_pos}",
                "Owner": roster_name(sp.owner_roster_id) + (" (you)" if sp.owner_roster_id == my_roster_id else ""),
                "Pos": proj.position if proj else "?",
                "Player": proj.name if proj else (sp.player_id or "?"),
                "Team": (proj.team if proj else "—") or "FA",
                "Age": (proj.age if proj else None) or "—",
                "Traded": "yes" if sp.is_traded else "",
            })
        st.dataframe(recent_rows, hide_index=True, width="stretch", height=min(420, 50 + 36 * len(recent_rows)))
    else:
        st.info("Draft hasn't started.")

    # ----- Forward simulator (clean table) -----
    st.markdown('<div class="sec-head"><h2>Forward simulator</h2></div>', unsafe_allow_html=True)
    ctl1, ctl2 = st.columns([3, 1])
    with ctl1:
        sim_source = st.radio(
            "Prediction source",
            ["Consensus — what they'll likely do (Sleeper SF dynasty ADP)",
             "Optimal — what they should do (our league-adjusted value)"],
            horizontal=False,
            label_visibility="collapsed",
        )
    with ctl2:
        sim_n = st.number_input("Look-ahead", min_value=5, max_value=40, value=20, step=5)
    source_key = "consensus" if sim_source.startswith("Consensus") else "optimal"

    predictions = simulate_next_picks(
        state=state, valued_all=valued_all, projections=projections,
        cfg=cfg, n=int(sim_n), ranking_source=source_key,
    )
    n_mismatches = sum(1 for p in predictions if p.alt_player_id is not None)
    if predictions:
        st.caption(
            f"Mode: {sim_source.split('—')[0].strip()}. "
            f"{n_mismatches} of {len(predictions)} predictions differ between the two engines — those gaps are where "
            f"the league is mispricing players."
        )
        on_clock = state.on_clock()
        sim_rows = []
        alt_label_header = "Our model says" if source_key == "consensus" else "Consensus says"
        for p in predictions:
            you = " (you)" if p.roster_id == my_roster_id else ""
            on_clock_mark = "ON CLOCK" if (on_clock and p.pick_no == on_clock.pick_no) else ""
            adp = f"{p.consensus_rank:.1f}" if p.consensus_rank is not None and p.consensus_rank < 500 else "—"
            alt = (
                f"{p.alt_player_name} ({p.alt_position}, #{p.alt_rank})"
                if p.alt_player_id else ""
            )
            sim_rows.append({
                "Pick": p.pick_no,
                "Rd": f"R{p.round}.{p.slot_pos}",
                "Status": on_clock_mark,
                "Owner": roster_name(p.roster_id) + you,
                "Stance": p.inferred_stance,
                "Pos": p.position,
                "Player": p.player_name,
                "Tm": p.team or "FA",
                "Age": p.age or "—",
                "Our rank": p.overall_rank,
                "ADP": adp,
                alt_label_header: alt,
                "Why": p.why,
            })
        st.dataframe(
            sim_rows, hide_index=True, width="stretch",
            height=min(640, 50 + 36 * len(sim_rows)),
            column_config={
                "Pick": st.column_config.NumberColumn(width="small"),
                "Our rank": st.column_config.NumberColumn(width="small"),
                "ADP": st.column_config.TextColumn(width="small"),
                "Stance": st.column_config.TextColumn(width="small"),
                "Why": st.column_config.TextColumn(width="large"),
            },
        )
    else:
        st.info("No upcoming picks to simulate.")
    st.caption(
        "Need weight: +40% for critical positional holes. Stance from mean drafted age "
        "(≤24 rebuild · 25-27 balanced · ≥28 compete). Picks cascade so a player predicted "
        "for pick 21 won't appear again at pick 28."
    )


# ------------------------------ Tab: Best Available ---------------------------

with tab_best:
    # VBD context: compute likely-available value at my next pick.
    my_picks = state.upcoming_for_roster(my_roster_id)
    on_clock_pick = state.on_clock()
    if my_picks and on_clock_pick:
        picks_until_mine = my_picks[0].pick_no - on_clock_pick.pick_no
    else:
        picks_until_mine = 0
    scarcity = positional_scarcity(state, projections)
    vctx = VBDContext(
        valued_undrafted=valued_undrafted,
        picks_until_my_next=max(0, picks_until_mine),
        positional_scarcity=scarcity,
    )

    st.markdown(
        f'<div class="sec-head"><h2>Best available</h2>'
        f'<div class="meta">strategy: {strategy} · horizon: {horizon}y · {picks_until_mine} picks until you · '
        f'{len(valued_undrafted)} undrafted players in the pool</div></div>',
        unsafe_allow_html=True,
    )
    filter_cols = st.columns([2, 3, 1])
    with filter_cols[0]:
        pos_filter = st.multiselect("Position", ["QB", "RB", "WR", "TE"],
                                    default=["QB", "RB", "WR", "TE"],
                                    label_visibility="collapsed")
    with filter_cols[1]:
        name_search = st.text_input("Search player", value="", label_visibility="collapsed",
                                    placeholder="Search by player name (e.g. dart, mahomes)")
    with filter_cols[2]:
        show_count = st.number_input("Show N", min_value=20, max_value=300, value=100, step=20,
                                     label_visibility="collapsed", help="How many rows to display")

    # Compute dynamic_top AFTER show_count is defined.
    dynamic_top = best_available_dynamic(vctx, top_n=int(show_count) + 50)

    name_q = name_search.strip().lower()
    filtered = [v for v in dynamic_top if v.position in pos_filter]
    if name_q:
        filtered = [v for v in filtered if name_q in v.name.lower()]
    filtered = filtered[: int(show_count)]
    likely = likely_next_available_value(vctx)
    consensus_idx_local = build_consensus_index(projections, superflex=cfg.superflex)
    has_ktc = any(v.ktc_value for v in filtered)
    rows = []
    for v in filtered:
        delta_next = round(v.dynasty_value - likely.get(v.position, 0.0), 1)
        adp = consensus_idx_local.get(v.player_id)
        adp_disp = round(adp, 1) if adp is not None and adp < 500 else None
        row = {
            "Rank": v.overall_rank,
            "Pos": v.position,
            "Name": v.name,
            "Team": v.team or "FA",
            "Age": v.age or "—",
            "Season pts": round(v.season_points),
            "Dyn": fmt_ba(v.pct_dynasty),
            "Dyn (raw)": round(v.dynasty_value),
        }
        if has_ktc:
            row["Comm"] = fmt_ba(v.pct_community)
            row["Fit"] = round(v.league_fit, 2) if v.league_fit else None
            row["Adj Comm"] = fmt_ba(v.pct_adj_community)
        row["ADP"] = adp_disp
        row["VBD"] = fmt_ba(v.pct_vbd)
        row["Dyn VBD"] = delta_next
        rows.append(row)
    if has_ktc:
        source_label = overlay_source or "Community"
        ktc_note = (
            f" · **{source_label} overlay active** — "
            "Adj community = Community value × league-fit multiplier"
        )
    else:
        ktc_note = (
            " · click **Fetch from FantasyCalc** in the sidebar (or upload a KTC CSV) "
            "to enable the community-value overlay"
        )
    st.caption(
        f"Showing {len(rows)} of {len(valued_undrafted)} undrafted players. "
        f"Use the search box to find a specific player; bump 'Show N' for a wider view.{ktc_note}"
    )
    # Per-column tooltips. Hover any header to see what the metric means.
    column_config = {
        "Rank": st.column_config.NumberColumn(
            "Rank",
            help="Overall rank across all skill-position players, sorted by Dynasty value.",
        ),
        "Pos": st.column_config.TextColumn("Pos", help="Fantasy position."),
        "Age": st.column_config.TextColumn("Age", help="Current age (years)."),
        "Season pts": st.column_config.NumberColumn(
            "Season pts",
            help="Projected fantasy points for the upcoming season computed against "
                 "YOUR exact scoring_settings (6pt pass TDs, completion bonus, first-down "
                 "bonuses, TE Premium, etc.). The headline number that nothing else in the "
                 "industry computes for your specific league.",
            format="%d",
        ),
        "Dyn": st.column_config.TextColumn(
            "Dyn",
            help="Dynasty value rank on a 0.000–1.000 scale. 1.000 = top of pool, "
                 ".500 = median, .000 = bottom. The number means the same thing across "
                 "every column — direct comparison works.",
        ),
        "Dyn (raw)": st.column_config.NumberColumn(
            "Dyn (raw)",
            help="Raw Dynasty value: multi-year discounted Season pts × age curve × "
                 "strategy weights. ~1000-1500 for elite players. Useful when you want the "
                 "magnitude difference between two players, not just the relative ranking.",
            format="%d",
        ),
        "Comm": st.column_config.TextColumn(
            "Comm",
            help="Community value rank on a 0.000–1.000 scale, computed across players with "
                 "market data. Source: FantasyCalc or KTC depending on what's loaded.",
        ),
        "Fit": st.column_config.NumberColumn(
            "Fit",
            help="League-fit multiplier. 1.00 = position-median scoring fit. 1.20 = 20%% above "
                 "median (your scoring rewards this player extra).",
            format="%.2f",
        ),
        "Adj Comm": st.column_config.TextColumn(
            "Adj Comm",
            help="Adjusted Community (Community × Fit) on a 0.000–1.000 scale. The community "
                 "consensus re-tilted for YOUR scoring system. Compare to Comm to spot where "
                 "your league rewards or penalizes vs the market.",
        ),
        "ADP": st.column_config.NumberColumn(
            "ADP",
            help="Sleeper Superflex dynasty Average Draft Position — what the average drafter "
                 "would take this player at. Lower = picked earlier.",
            format="%.1f",
        ),
        "VBD": st.column_config.TextColumn(
            "VBD",
            help="Value Based Drafting on a 0.000–1.000 scale. How much more this player is "
                 "worth than the replacement-level player at their position (QB24 SF, RB36, "
                 "WR48, TE18), as a rank. Higher = harder to replace at their spot.",
        ),
        "Dyn VBD": st.column_config.NumberColumn(
            "Dyn VBD",
            help="Dynamic VBD. Value above the player who'll LIKELY still be on the board "
                 "at your NEXT pick. Positive = take now (you can't wait). Negative = wait, "
                 "better options exist at other positions this pick.",
            format="%d",
        ),
    }
    st.dataframe(
        rows, hide_index=True, width="stretch",
        height=min(640, 50 + 36 * min(len(rows), 18)),
        column_config=column_config,
    )
    st.caption("**Dynamic VBD** = dynasty_value − value of the player likely available at your next pick. "
               "Positive = take now. Negative = you can probably wait at that position.")

    # ---------- PLAYER DRILL-DOWN (Robinhood-style detail card) ----------
    # Pick any player from the filtered list above and see the full breakdown:
    # scoring components, dynasty trajectory, where they'd land in the market,
    # and the closest comparable peers at their position.
    st.markdown('<div class="sec-head"><h2>Player detail</h2>'
                '<div class="meta">drill into the math behind any player</div></div>',
                unsafe_allow_html=True)

    detail_options = [(v.player_id, f"{v.name} · {v.position}{v.position_rank} · {v.team or 'FA'}") for v in filtered]
    if not detail_options:
        st.info("No players match the filters above.")
    else:
        # Default to the top-ranked filtered player so the panel never starts empty.
        sel_pid = st.selectbox(
            "Select player",
            detail_options,
            format_func=lambda x: x[1],
            key="ranks_detail_pick",
            label_visibility="collapsed",
        )[0]
        v = next((x for x in valued_all if x.player_id == sel_pid), None)
        proj = projections.get(sel_pid)
        if v is not None:
            # ----- Header card -----
            adp = consensus_idx_local.get(v.player_id)
            adp_disp = f"{adp:.1f}" if (adp is not None and adp < 500) else "—"
            comm_raw = (
                v.adjusted_ktc or v.ktc_value or v.dp_value_2qb or v.dp_value_1qb or 0
            )
            st.markdown(
                f"""
                <div class="rh-row" style="padding: 1rem 1.2rem;">
                  <div style="display:flex; justify-content:space-between; align-items:baseline; gap:1rem;">
                    <div>
                      <div style="font-size:1.25rem; font-weight:700; color:var(--ink); letter-spacing:-0.01em;">
                        {pos_chip(v.position)} &nbsp; {v.name}
                      </div>
                      <div class="meta" style="margin-top:4px;">
                        {v.team or 'FA'} · age {v.age or '—'} · rank #{v.overall_rank} overall ·
                        {v.position}{v.position_rank}
                      </div>
                    </div>
                    <div style="text-align:right;">
                      <div style="font-size:0.62rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.09em; font-weight:600;">Dynasty (your league)</div>
                      <div class="ba" style="font-size:1.65rem;">{fmt_ba(v.pct_dynasty)}</div>
                    </div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # ----- Stat grid -----
            stat_cells = [
                ("Season pts", f"{round(v.season_points)}"),
                ("Dynasty raw", f"{round(v.dynasty_value)}"),
                ("VBD", fmt_ba(v.pct_vbd)),
                ("Community", fmt_ba(v.pct_community) if v.pct_community is not None else "—"),
                ("Adj community", fmt_ba(v.pct_adj_community) if v.pct_adj_community is not None else "—"),
                ("Fit ×", f"{v.league_fit:.2f}" if v.league_fit else "—"),
                ("Market value", f"{round(comm_raw):,}" if comm_raw else "—"),
                ("Consensus ADP", adp_disp),
            ]
            grid_html = "<div class='stat-grid'>" + "".join(
                f"<div class='stat-cell'><div class='k'>{k}</div><div class='v'>{val}</div></div>"
                for k, val in stat_cells
            ) + "</div>"
            st.markdown(grid_html, unsafe_allow_html=True)

            # ----- Scoring components (what built the season points) -----
            if proj:
                comps = explain_scoring_components(proj.stats, cfg, top_n=8)
                if comps:
                    st.markdown("##### Scoring breakdown")
                    comp_rows = [{"Component": label, "Points": round(pts, 1)} for label, pts in comps]
                    st.dataframe(
                        comp_rows, hide_index=True, width="stretch",
                        height=min(360, 50 + 36 * len(comp_rows)),
                    )

            # ----- Comparables — same position, nearest dynasty value -----
            peers = sorted(
                [x for x in valued_all if x.position == v.position and x.player_id != v.player_id],
                key=lambda x: abs(x.dynasty_value - v.dynasty_value),
            )[:6]
            if peers:
                st.markdown("##### Closest comparables at position")
                peer_rows = [
                    {
                        "Rank": p.overall_rank,
                        "Player": p.name,
                        "Team": p.team or "FA",
                        "Age": p.age or "—",
                        "Dyn": fmt_ba(p.pct_dynasty),
                        "Dyn (raw)": round(p.dynasty_value),
                        "Δ vs " + v.name.split()[-1]: round(p.dynasty_value - v.dynasty_value),
                    }
                    for p in peers
                ]
                st.dataframe(
                    peer_rows, hide_index=True, width="stretch",
                    height=min(280, 50 + 36 * len(peer_rows)),
                )

    # ---------- BUY-LOW ----------
    st.markdown('<div class="sec-head"><h2>Buy-low watchlist</h2>'
                '<div class="meta">your rank ≪ consensus ADP — they\'ll let these slide</div></div>',
                unsafe_allow_html=True)

    consensus_idx = build_consensus_index(projections, superflex=cfg.superflex)
    buylow_rows = []
    for v in valued_undrafted[:120]:
        adp = consensus_idx.get(v.player_id, 9999)
        if adp >= 500:
            continue
        gap = adp - v.overall_rank
        if gap < 10:
            continue
        buylow_rows.append({
            "Our rank": v.overall_rank,
            "Pos": v.position,
            "Player": v.name,
            "Tm": v.team or "FA",
            "Age": v.age or "—",
            "TW value": round(v.dynasty_value),
            "Consensus ADP": round(adp, 1),
            "Gap": round(gap, 1),
        })
    buylow_rows.sort(key=lambda r: -r["Gap"])
    st.dataframe(buylow_rows[:15], hide_index=True, width="stretch",
                 height=min(540, 50 + 36 * min(15, len(buylow_rows))))

    # ---------- SELL-HIGH ----------
    st.markdown('<div class="sec-head"><h2>Sell-high (your roster)</h2>'
                '<div class="meta">consensus rates them above your model — ask premium</div></div>',
                unsafe_allow_html=True)
    my_player_ids = {str(p["player_id"]) for p in state.picks if int(p.get("roster_id") or 0) == my_roster_id}
    sellhigh_rows = []
    for v in valued_all:
        if v.player_id not in my_player_ids:
            continue
        adp = consensus_idx.get(v.player_id, 9999)
        if adp >= 500:
            continue
        gap = v.overall_rank - adp
        sellhigh_rows.append({
            "Our rank": v.overall_rank,
            "Pos": v.position,
            "Player": v.name,
            "TW value": round(v.dynasty_value),
            "Consensus ADP": round(adp, 1),
            "Gap (overvalued by mkt)": round(gap, 1),
        })
    sellhigh_rows.sort(key=lambda r: -r["Gap (overvalued by mkt)"])
    if sellhigh_rows:
        st.dataframe(sellhigh_rows, hide_index=True, width="stretch")
    else:
        st.info("No clear sell-high candidates yet.")


# ------------------------------ Tab: My Team ----------------------------------

with tab_team:
    me = team_summary(state, my_roster_id, projections)
    st.markdown('<div class="sec-head"><h2>Roster construction</h2>'
                f'<div class="meta">target depth in 12-team SF dynasty</div></div>',
                unsafe_allow_html=True)
    cols = st.columns(4)
    for i, pos in enumerate(["QB", "RB", "WR", "TE"]):
        with cols[i]:
            have = me.counts[pos]
            want = me.depth_targets[pos]
            need = me.needs[pos]
            st.metric(pos, f"{have} / {want}", delta=f"{need:.2f} need", delta_color="off")
            st.progress(min(1.0, have / max(want, 1)))

    st.markdown('<div class="sec-head"><h2>Drafted players</h2></div>', unsafe_allow_html=True)
    if me.drafted_players:
        team_rows = []
        for p in sorted(me.drafted_players, key=lambda x: -x.league_points):
            team_rows.append({
                "Pos": p.position,
                "Player": p.name,
                "Tm": p.team or "FA",
                "Age": p.age or "—",
                "TW pts (season)": round(p.league_points, 1),
                "PPG (TW)": points_per_game(p),
                "Sleeper PPR": round(p.sleeper_pts_ppr, 1),
            })
        st.dataframe(team_rows, hide_index=True, width="stretch",
                     height=min(420, 50 + 36 * len(team_rows)))
    else:
        st.info("No picks made yet.")

    st.markdown('<div class="sec-head"><h2>Your upcoming picks</h2><div class="meta">3RR applied</div></div>',
                unsafe_allow_html=True)
    upcoming_rows = []
    for sp in state.upcoming_for_roster(my_roster_id)[:14]:
        gap = sp.pick_no - (on_clock_pick.pick_no if on_clock_pick else sp.pick_no)
        upcoming_rows.append({
            "Pick": sp.pick_no,
            "Round.Pos": f"R{sp.round}.{sp.slot_pos}",
            "Picks until": gap,
            "Source": "traded" if sp.is_traded else "own",
        })
    st.dataframe(upcoming_rows, hide_index=True, width="stretch",
                 height=min(540, 50 + 36 * len(upcoming_rows)))


# ------------------------------ Tab: Tier Alerts ------------------------------

with tab_tiers:
    # ----- Metric selector for the entire tab -----
    # Build a dict of (label → (value_fn, value_formatter)). Options that depend on
    # KTC overlay or VBD context are only added when the underlying data is present.
    has_community = any(v.ktc_value for v in valued_all)
    has_adj_community = any(v.adjusted_ktc for v in valued_all)

    # Dynamic VBD requires the VBDContext. We computed picks_until_mine + scarcity
    # earlier in the Best Available tab as `vctx`; compute dyn_vbd here from it.
    from src.vbd import dynamic_vbd  # local import — keeps top of file clean
    dyn_vbd_map = dynamic_vbd(vctx) if 'vctx' in globals() else {}

    # For Dyn VBD, compute a percentile rank on the fly (it's not stored on ValuedPlayer)
    from src.normalize import percentile_rank as _pct_rank
    dyn_vbd_pct = _pct_rank(dyn_vbd_map) if dyn_vbd_map else {}

    tier_metric_options: dict[str, tuple] = {
        "Dynasty value (our model)": (
            lambda p: float(p.dynasty_value or 0),
            lambda p: fmt_ba(p.pct_dynasty),
        ),
        "VBD (over replacement)": (
            lambda p: float(p.replacement_delta or 0),
            lambda p: fmt_ba(p.pct_vbd),
        ),
    }
    if has_community:
        tier_metric_options["Community value"] = (
            lambda p: float(p.ktc_value or 0),
            lambda p: fmt_ba(p.pct_community),
        )
    if has_adj_community:
        tier_metric_options["Adj community (KTC × fit)"] = (
            lambda p: float(p.adjusted_ktc or 0),
            lambda p: fmt_ba(p.pct_adj_community),
        )
    if dyn_vbd_pct:
        tier_metric_options["Dyn VBD (vs likely next available)"] = (
            lambda p: float(dyn_vbd_map.get(p.player_id, 0) or 0),
            lambda p: fmt_ba(dyn_vbd_pct.get(p.player_id)),
        )

    metric_choice = st.selectbox(
        "Group tiers by",
        list(tier_metric_options.keys()),
        index=0,
        help="Switch the metric used for both tier grouping AND the value displayed "
             "next to each player. Compare how tier breaks shift across value systems.",
    )
    value_fn, value_fmt = tier_metric_options[metric_choice]

    tiers = detect_tiers(valued_all, value_fn=value_fn)
    alerts = tier_cliff_alerts(tiers, drafted_ids)

    st.markdown(f'<div class="sec-head"><h2>Tier cliff alerts</h2>'
                f'<div class="meta">{len(alerts)} active · grouped by {metric_choice.lower()}</div></div>',
                unsafe_allow_html=True)
    if alerts:
        alert_rows = [{
            "Pos": a.position,
            "Tier": f"T{a.tier_number}",
            "Players left": a.remaining,
            "Severity": a.severity,
        } for a in alerts]
        st.dataframe(alert_rows, hide_index=True, width="stretch",
                     height=min(360, 50 + 36 * len(alert_rows)))
    else:
        st.info("No tier cliffs currently within threshold.")

    st.markdown(f'<div class="sec-head"><h2>Tiers by position</h2>'
                f'<div class="meta">metric: {metric_choice} · drafted players struck through</div></div>',
                unsafe_allow_html=True)
    MAX_LINES_PER_TIER = 20
    pcol = st.columns(4)
    for i, pos in enumerate(["QB", "RB", "WR", "TE"]):
        with pcol[i]:
            st.markdown(f"##### {pos}", unsafe_allow_html=True)
            for tier in tiers.get(pos, [])[:6]:
                undrafted_in_tier = [p for p in tier.players if p.player_id not in drafted_ids]
                shown = tier.players[:MAX_LINES_PER_TIER]
                hidden_count = max(0, len(tier.players) - len(shown))
                lines = []
                for p in shown:
                    drafted = p.player_id in drafted_ids
                    mark = '<span style="color:#94a3b8;text-decoration:line-through">' if drafted else '<span>'
                    lines.append(
                        f'{mark}{p.name}</span> '
                        f'<span style="color:var(--muted-2);font-variant-numeric:tabular-nums">{value_fmt(p)}</span>'
                    )
                footer = (
                    f'<div style="color:var(--muted-2);font-size:0.7rem;margin-top:2px">+ {hidden_count} more (lower value)</div>'
                    if hidden_count else ""
                )
                st.markdown(
                    f'<div style="font-size:0.75rem;margin-bottom:0.6rem;">'
                    f'<div style="font-weight:600;color:var(--ink-2);margin-bottom:2px">'
                    f'T{tier.tier_number} <span style="color:var(--muted);font-weight:500">'
                    f'· {len(undrafted_in_tier)}/{tier.size} left</span></div>'
                    + "<br>".join(lines) +
                    footer +
                    "</div>",
                    unsafe_allow_html=True,
                )


# ------------------------------ Tab: Trade Targets ----------------------------

with tab_trade:
    # Build the pick value model once
    try:
        dp_values_df = dl.values()
        pick_model = build_pick_value_model(valued_all, dp_values_df, cfg)
    except Exception as e:
        st.error(f"DynastyProcess data unavailable; can't price picks. {e}")
        pick_model = None

    needs_all = league_needs(state, projections)

    # Parse transaction history for trade-activity weighting (used below + by recommender)
    player_name_fn = lambda pid: (projections[pid].name if pid in projections else f"player {pid}")
    trades_history = parse_trades(transactions_raw, roster_name_fn=roster_name)
    activity_counts = activity_per_roster(trades_history)

    # ---------- ACQUISITION PLANNER ----------
    st.markdown('<div class="sec-head"><h2>Acquisition planner</h2>'
                '<div class="meta">pick what you want — see what it costs</div></div>',
                unsafe_allow_html=True)
    st.caption(
        "Reverse trade finder. Tell it which player(s) and/or pick(s) you want from a "
        "specific partner; it returns the cheapest viable packages you can offer to "
        "get them. Ranked by YOUR value gain (best for you first), filtered to trades "
        "the partner would plausibly accept."
    )

    if not pick_model:
        st.info("DynastyProcess values not loaded — acquisition planner needs the data layer to be online.")
    else:
        # ---- Partner selection ----
        partner_rids_all = sorted(set(state.slot_to_roster.values()) - {my_roster_id})
        partner_labels = [roster_name(rid) for rid in partner_rids_all]
        ap_col_partner, ap_col_results = st.columns([1, 2])
        with ap_col_partner:
            target_partner_label = st.selectbox(
                "Partner",
                partner_labels,
                key="ap_partner",
                help="Which manager you want to acquire from.",
            )
            target_partner_rid = partner_rids_all[partner_labels.index(target_partner_label)]

            # Their drafted players
            their_picks_made = [p for p in state.picks if int(p.get("roster_id") or 0) == target_partner_rid]
            their_player_options = []
            for pk in their_picks_made:
                pid = str(pk.get("player_id") or "")
                if pid in projections:
                    proj = projections[pid]
                    their_player_options.append((pid, f"{proj.position} {proj.name} ({proj.team or 'FA'})"))
            target_player_ids = st.multiselect(
                "Their drafted players you want",
                their_player_options,
                format_func=lambda x: x[1],
                key="ap_target_players",
            )

            # Their unmade startup picks
            their_unmade_sps = sorted(
                [sp for sp in state.schedule if not sp.made and sp.owner_roster_id == target_partner_rid],
                key=lambda sp: sp.pick_no,
            )[:8]
            sp_options = [(sp.pick_no, f"pick {sp.pick_no} (R{sp.round}.{sp.slot_pos})") for sp in their_unmade_sps]
            target_startup_picks = st.multiselect(
                "Their upcoming startup picks",
                sp_options,
                format_func=lambda x: x[1],
                key="ap_target_sps",
            )

            # Their future rookie picks
            their_future_picks: list[tuple[str, int, int]] = []
            future_pick_options = []
            for season in ("2027", "2028"):
                for p in state.future_pick_inventory(season).get(target_partner_rid, []):
                    key = (season, p["round"], p["orig_roster_id"])
                    via = "" if p["orig_roster_id"] == target_partner_rid else f" (via {roster_name(p['orig_roster_id'])})"
                    future_pick_options.append((key, f"{season} R{p['round']}{via}"))
            target_future_picks = st.multiselect(
                "Their future rookie picks",
                future_pick_options,
                format_func=lambda x: x[1],
                key="ap_target_futures",
            )

            max_give = st.slider(
                "Max assets per offer",
                min_value=1, max_value=5, value=4,
                key="ap_max_give",
                help="Largest package size to consider on YOUR side. Recent trades in your "
                     "league have run as deep as 4-for-2. Bump higher for big trade-up packages.",
            )

        # ---- Run planner + render results ----
        with ap_col_results:
            if not (target_player_ids or target_startup_picks or target_future_picks):
                st.info(
                    "Pick at least one asset on the left, then results will appear here. "
                    "You can mix players, upcoming startup picks, and future rookie picks in one ask."
                )
            else:
                target_player_id_list = [pid for pid, _ in target_player_ids]
                target_sp_list = [pn for pn, _ in target_startup_picks]
                target_fp_list = [tup for tup, _ in target_future_picks]

                options = plan_acquisition(
                    state=state, valued_all=valued_all, projections=projections,
                    pick_model=pick_model, cfg=cfg,
                    my_roster_id=my_roster_id,
                    partner_rid=target_partner_rid,
                    target_player_ids=target_player_id_list,
                    target_picks=target_fp_list,
                    target_startup_picks=target_sp_list,
                    roster_name_fn=roster_name,
                    max_combo_size=int(max_give),
                    max_results=10,
                )

                if not options:
                    st.warning(
                        "No viable packages found. Either you don't have enough trade-able "
                        "assets to satisfy this ask, or the partner would reject every "
                        "combination. Try selecting fewer / less-premium targets."
                    )
                else:
                    st.caption(f"Showing {len(options)} viable package{'s' if len(options) > 1 else ''}, "
                               "cheapest to YOU first.")
                    for i, opt in enumerate(options, start=1):
                        v_class = "send" if opt.eval.combined in ("SEND IT", "SEND") else (
                            "offer" if opt.eval.combined.startswith("OFFER") or "FAIR" in opt.eval.combined else "pass"
                        )
                        head = (
                            f"#{i}  ·  Offer: {opt.summary_line}  ·  "
                            f"You {opt.eval.tw_delta:+.0f}  ·  {opt.eval.combined}"
                        )
                        with st.expander(head, expanded=(i == 1)):
                            kc1, kc2, kc3 = st.columns(3)
                            kc1.metric("Your gain", f"{opt.eval.tw_delta:+.0f}",
                                       help="Trade delta in YOUR league-adjusted value.")
                            kc2.metric("Market gain", f"{opt.eval.consensus_delta_for_them:+.0f}",
                                       help="What the partner sees on their side.")
                            kc3.metric("Verdict", opt.eval.combined.split("(")[0].strip())

                            cg, cr = st.columns(2)
                            with cg:
                                st.markdown("**You'd give**")
                                for a in opt.give:
                                    if isinstance(a, PlayerAsset):
                                        st.markdown(
                                            f"&middot; {pos_chip(a.position)} **{a.name}** "
                                            f"<span style='color:var(--muted);font-size:0.72rem'>"
                                            f"You {a.tw_value:.0f} · Market {a.consensus_value:.0f}</span>",
                                            unsafe_allow_html=True,
                                        )
                                    else:
                                        st.markdown(
                                            f"&middot; {a.label} "
                                            f"<span style='color:var(--muted);font-size:0.72rem'>"
                                            f"You {a.tw_value:.0f} · Market {a.consensus_value:.0f}</span>",
                                            unsafe_allow_html=True,
                                        )
                            with cr:
                                st.markdown(f"**You get** (from {target_partner_label})")
                                for a in opt.get:
                                    if isinstance(a, PlayerAsset):
                                        st.markdown(
                                            f"&middot; {pos_chip(a.position)} **{a.name}** "
                                            f"<span style='color:var(--muted);font-size:0.72rem'>"
                                            f"You {a.tw_value:.0f} · Market {a.consensus_value:.0f}</span>",
                                            unsafe_allow_html=True,
                                        )
                                    else:
                                        st.markdown(
                                            f"&middot; {a.label} "
                                            f"<span style='color:var(--muted);font-size:0.72rem'>"
                                            f"You {a.tw_value:.0f} · Market {a.consensus_value:.0f}</span>",
                                            unsafe_allow_html=True,
                                        )

                            msg = build_offer_message(opt.eval, your_handle="you", their_handle=target_partner_label)
                            st.code(msg, language="text")

    # ---------- TRADE FINDER (auto-generated offers) ----------
    st.markdown('<div class="sec-head"><h2>Trade Finder</h2>'
                '<div class="meta">auto-generated · mutual-win scored</div></div>',
                unsafe_allow_html=True)
    st.caption(
        "Scans every partner and every plausible asset combination. Ranks by mutual-win "
        "— your value gain × the market-value gain on their side."
    )
    with st.expander("What do 'Your value' and 'Market value' mean?", expanded=False):
        st.markdown("""
**Your value** is our model's number — built from per-stat projections × your exact
league scoring (6pt pass TDs, completion bonus, first-down bonuses, TE Premium, RB rec
bonus, etc.) × position age curve × your selected strategy (compete/balanced/rebuild)
summed across your dynasty horizon. This is the TRUTH for your specific league.

**Market value** is what the trade partner is likely seeing — community consensus
dynasty values. The source is, in order of preference:

1. **FantasyCalc** (if you've clicked *Fetch latest from FantasyCalc* in the sidebar)
2. **KTC CSV** (if you've uploaded one)
3. **DynastyProcess** value_2qb (the built-in fallback)

For player picks: both sides use the same scale. For startup draft picks: the pick's
value is approximated by the consensus-ranked player likely to be available at that
slot. For 2027/2028 rookie picks: DynastyProcess pick values, scaled to match.

**The exploit** is when the two sides diverge. If you GAIN in Your value AND they
GAIN in Market value, it's a true mutual-win — they'll accept and you'll come out
ahead by your math. That's the trade you send.
""")

    # --- Recommender controls: search depth + partner filter + show-more ---
    ctrl_depth, ctrl_partner, ctrl_more = st.columns([2, 2, 1])
    with ctrl_depth:
        depth_choice = st.radio(
            "Search depth",
            ["Strict", "Normal", "Aggressive"],
            horizontal=True,
            help="Strict = high-confidence mutual-win only · Normal = wider net · "
                 "Aggressive = include marginal/creative packages. Higher depth = "
                 "more variety in the offer list AND more noise.",
        )
    depth_key = depth_choice.lower()
    depth_cfg = SEARCH_DEPTHS[depth_key]

    # Partner filter
    partner_rids = sorted(set(state.slot_to_roster.values()) - {my_roster_id})
    partner_labels = ["All partners"] + [roster_name(rid) for rid in partner_rids]
    with ctrl_partner:
        partner_pick = st.selectbox(
            "Force partner",
            partner_labels,
            help="Restrict the search to one specific partner. Useful when you want "
                 "to deeply explore offers with a single team.",
        )
    partner_filter_rid = None
    if partner_pick != "All partners":
        for rid in partner_rids:
            if roster_name(rid) == partner_pick:
                partner_filter_rid = rid
                break

    # Show-more state: each depth has its own session counter so switching depth resets.
    sm_key = f"_show_more_offset_{depth_key}_{partner_filter_rid or 'all'}"
    if sm_key not in st.session_state:
        st.session_state[sm_key] = 0
    extra = st.session_state[sm_key]
    effective_max = int(depth_cfg["max_total"]) + extra

    with ctrl_more:
        if st.button("Show more candidates", help=f"Reveal {int(depth_cfg['max_total'])} more offers below the current top list."):
            st.session_state[sm_key] = extra + int(depth_cfg["max_total"])
            st.rerun()

    if pick_model:
        offers = recommend_offers(
            state=state, valued_all=valued_all, projections=projections,
            pick_model=pick_model, cfg=cfg, my_roster_id=my_roster_id,
            roster_name_fn=roster_name,
            activity_per_roster=activity_counts,
            min_tw_gain=float(depth_cfg["min_tw_gain"]),
            max_per_partner=int(depth_cfg["max_per_partner"]),
            max_total=effective_max,
            top_their_players=int(depth_cfg["top_their_players"]),
            partner_filter=partner_filter_rid,
        )
    else:
        offers = []

    if not offers:
        st.info("No high-confidence offers right now. Try again after the next few picks.")
    else:
        st.caption(f"{len(offers)} candidate offer{'s' if len(offers) > 1 else ''} — expand any row to see detail and copy message.")
        for i, o in enumerate(offers, start=1):
            verdict_class = "send" if o.eval.combined in ("SEND IT", "SEND") else (
                "offer" if o.eval.combined.startswith("OFFER") or "FAIR" in o.eval.combined else "pass"
            )
            head = f"#{i} → {o.partner_name} · {o.eval.combined} · Your value {o.eval.tw_delta:+.0f}"
            with st.expander(head, expanded=(i == 1)):
                c_give, c_get, c_kpi = st.columns([2, 2, 1])
                # Helper: render one asset row with batting averages and raw values
                def _asset_line(a):
                    if isinstance(a, PlayerAsset):
                        vp = next((x for x in valued_all if x.player_id == a.player_id), None)
                        you_ba = fmt_ba(vp.pct_dynasty) if vp else "—"
                        mkt_ba = fmt_ba(vp.pct_community if (vp and vp.pct_community is not None) else None)
                        return (
                            f"&middot; {pos_chip(a.position)} **{a.name}** "
                            f"<span style='color:var(--muted);font-size:0.72rem'>"
                            f"You <b>{you_ba}</b> ({a.tw_value:.0f}) · "
                            f"Market <b>{mkt_ba}</b> ({a.consensus_value:.0f})</span>"
                        )
                    return (
                        f"&middot; {a.label} "
                        f"<span style='color:var(--muted);font-size:0.72rem'>"
                        f"You {a.tw_value:.0f} · Market {a.consensus_value:.0f}</span>"
                    )

                with c_give:
                    st.markdown("**You give**")
                    for a in o.give:
                        st.markdown(_asset_line(a), unsafe_allow_html=True)
                with c_get:
                    st.markdown(f"**You get** (from {o.partner_name})")
                    for a in o.get:
                        st.markdown(_asset_line(a), unsafe_allow_html=True)
                with c_kpi:
                    st.metric("Your gain", f"{o.eval.tw_delta:+.0f}",
                              help="Trade delta in YOUR model (per-stat × your scoring × age curve).")
                    st.metric("Market gain", f"{o.eval.consensus_delta_for_them:+.0f}",
                              help="Trade delta in the MARKET (community/FantasyCalc/KTC value) — "
                                   "what the partner sees on their side. Positive = they think they win.")

                st.markdown(f"<div class='kv'><span class='k'>Why</span>{o.rationale}</div>",
                            unsafe_allow_html=True)
                msg = build_offer_message(o.eval, your_handle="you", their_handle=o.partner_name)
                st.code(msg, language="text")

    # ---------- TRADE CALCULATOR ----------
    st.markdown('<div class="sec-head"><h2>Manual trade calculator</h2>'
                '<div class="meta">build your own and see the verdict</div></div>',
                unsafe_allow_html=True)
    st.caption(
        "Every asset priced twice — your league-adjusted value and the consensus "
        "value the partner is seeing. The gap between them is your exploit."
    )

    # Helpers to build option pools
    def picks_for_roster(rid: int, max_round: int = 4) -> list[dict]:
        out = []
        for season in ("2027", "2028"):
            for p in state.future_pick_inventory(season).get(rid, []):
                if p["round"] <= max_round:
                    out.append({**p, "season": season})
        return out

    your_picks = picks_for_roster(my_roster_id)
    your_player_picks = [p for p in state.picks if int(p.get("roster_id") or 0) == my_roster_id]
    your_player_ids = [str(p["player_id"]) for p in your_player_picks]

    # Partner selector
    partner_options = [(rid, roster_name(rid)) for rid in state.slot_to_roster.values() if rid != my_roster_id]
    partner_options.sort(key=lambda x: x[1].lower())
    partner_choice = st.selectbox(
        "Trade partner",
        partner_options,
        format_func=lambda x: x[1],
        index=next(
            (i for i, (rid, _) in enumerate(partner_options) if roster_name(rid).lower() in {"dpurtee", "jimmack"}),
            0,
        ),
    )
    partner_rid = partner_choice[0]
    partner_picks = picks_for_roster(partner_rid)
    partner_player_picks = [p for p in state.picks if int(p.get("roster_id") or 0) == partner_rid]
    partner_player_ids = [str(p["player_id"]) for p in partner_player_picks]

    # Build asset labels for multiselect
    def player_label(pid: str) -> str:
        v = next((x for x in valued_all if x.player_id == pid), None)
        if not v: return pid
        return f"{v.name} ({v.position}, {v.team or 'FA'}) — TW {v.dynasty_value:.0f}"

    def pick_label(p: dict) -> str:
        season = p["season"]; rnd = p["round"]; orig = p["orig_roster_id"]
        suffix = f" (via {roster_name(orig)})" if orig != my_roster_id and orig != partner_rid else ""
        return f"{season} R{rnd}{suffix}"

    your_player_options = [(pid, player_label(pid)) for pid in your_player_ids]
    your_pick_options = [(("pick", p["season"], p["round"], p["orig_roster_id"]), pick_label(p)) for p in your_picks]
    partner_player_options = [(pid, player_label(pid)) for pid in partner_player_ids]
    partner_pick_options = [
        (("pick", p["season"], p["round"], p["orig_roster_id"]), pick_label(p))
        for p in partner_picks
    ]

    col_give, col_get = st.columns(2)
    with col_give:
        st.markdown("##### You give")
        give_players = st.multiselect(
            "Players", your_player_options, format_func=lambda x: x[1], key="give_players",
        )
        give_picks_sel = st.multiselect(
            "Picks", your_pick_options, format_func=lambda x: x[1], key="give_picks",
        )
    with col_get:
        st.markdown(f"##### You get (from {roster_name(partner_rid)})")
        get_players = st.multiselect(
            "Players", partner_player_options, format_func=lambda x: x[1], key="get_players",
        )
        get_picks_sel = st.multiselect(
            "Picks", partner_pick_options, format_func=lambda x: x[1], key="get_picks",
        )

    # Build asset lists
    give_assets, get_assets = [], []
    for pid, _ in give_players:
        a = player_asset(pid, valued_all)
        if a: give_assets.append(a)
    for pid, _ in get_players:
        a = player_asset(pid, valued_all)
        if a: get_assets.append(a)
    if pick_model:
        for (_, season, rnd, orig), label in give_picks_sel:
            a = pick_asset(season, rnd, pick_model, original_roster_id=orig, label=pick_label({
                "season": season, "round": rnd, "orig_roster_id": orig
            }))
            if a: give_assets.append(a)
        for (_, season, rnd, orig), label in get_picks_sel:
            a = pick_asset(season, rnd, pick_model, original_roster_id=orig, label=pick_label({
                "season": season, "round": rnd, "orig_roster_id": orig
            }))
            if a: get_assets.append(a)

    # Evaluate
    if give_assets or get_assets:
        result = evaluate_trade(give=give_assets, get=get_assets, cfg=cfg, my_roster_id=my_roster_id)

        # Verdict banner
        verdict_class = "send" if result.combined in ("SEND IT", "SEND") else (
            "offer" if result.combined in ("OFFER (expect counter)", "FAIR SWAP", "FAIR (for you)") else "pass"
        )
        st.markdown(
            f'<div class="verdict {verdict_class}">'
            f'<div class="label">Verdict</div>'
            f'<div class="text">{result.combined}</div>'
            f'<div class="sub">Your value: {result.tw_delta:+.0f} &middot; '
            f'Market value (theirs): {result.consensus_delta_for_them:+.0f} &middot; '
            f'Their likely response: {result.their_response}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Side-by-side breakdown
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            st.markdown("**Your side (you give):**")
            for a in give_assets:
                if hasattr(a, "name"):
                    st.write(f"  · {a.name} ({a.position}) — TW {a.tw_value:.0f} · consensus {a.consensus_value:.0f}")
                else:
                    st.write(f"  · {a.label} — TW {a.tw_value:.0f} · consensus {a.consensus_value:.0f}")
            st.write(f"  **TW total:** {result.give_tw_total:.0f}")
            st.write(f"  **Consensus total:** {result.give_consensus_total:.0f}")
        with bcol2:
            st.markdown("**Their side (you get):**")
            for a in get_assets:
                if hasattr(a, "name"):
                    st.write(f"  · {a.name} ({a.position}) — TW {a.tw_value:.0f} · consensus {a.consensus_value:.0f}")
                else:
                    st.write(f"  · {a.label} — TW {a.tw_value:.0f} · consensus {a.consensus_value:.0f}")
            st.write(f"  **TW total:** {result.get_tw_total:.0f}")
            st.write(f"  **Consensus total:** {result.get_consensus_total:.0f}")

        for note in result.notes:
            st.write(note)

        # Message generator
        st.markdown("**Copy-friendly Sleeper-chat offer:**")
        msg = build_offer_message(result, your_handle="tbanks44", their_handle=roster_name(partner_rid))
        st.code(msg, language="text")

    st.divider()

    # ---------- INVENTORY + REBUILDER LIST ----------
    inv_col1, inv_col2 = st.columns([1, 1])
    with inv_col1:
        st.markdown('<div class="sec-head"><h2>Your future inventory</h2></div>', unsafe_allow_html=True)
        inv_rows = []
        for season in ("2027", "2028"):
            for p in state.future_pick_inventory(season).get(my_roster_id, []):
                label = f"{season} R{p['round']}"
                origin = "own" if p["orig_roster_id"] == my_roster_id else f"via {roster_name(p['orig_roster_id'])}"
                tw = "—"
                consensus = "—"
                if pick_model:
                    pv = pick_model.get(season, p["round"])
                    if pv:
                        tw = round(pv.tw_value)
                        consensus = round(pv.consensus_value)
                inv_rows.append({"Pick": label, "Origin": origin, "TW value": tw, "Consensus": consensus})
        st.dataframe(inv_rows, hide_index=True, width="stretch",
                     height=min(420, 50 + 36 * len(inv_rows)))

    with inv_col2:
        st.markdown('<div class="sec-head"><h2>Likely trade partners</h2>'
                    '<div class="meta">ranked by 2027 holdings · activity from past trades</div></div>',
                    unsafe_allow_html=True)
        inv_all = state.future_pick_inventory("2027")
        partner_rows = []
        for rid, picks_list in sorted(inv_all.items(), key=lambda x: -len(x[1])):
            if rid == my_roster_id: continue
            their_needs = needs_all[rid].needs
            top_need = max(their_needs, key=their_needs.get) if their_needs else "?"
            stance = ("rebuilder" if len(picks_list) >= 5
                      else "competing" if len(picks_list) <= 2 else "neutral")
            partner_rows.append({
                "Team": roster_name(rid),
                "Stance": stance,
                "Trades done": activity_counts.get(rid, 0),
                "2027 picks held": len(picks_list),
                "Startup picks left": state.remaining_counts_per_roster().get(rid, 0),
                "Biggest hole": top_need,
            })
        st.dataframe(partner_rows[:8], hide_index=True, width="stretch",
                     height=min(420, 50 + 36 * min(8, len(partner_rows))))

    # ---------- LEAGUE TRADE HISTORY ----------
    st.markdown('<div class="sec-head"><h2>League trade history</h2>'
                f'<div class="meta">{len(trades_history)} completed trades · graded retroactively</div></div>',
                unsafe_allow_html=True)
    st.caption(
        "Every completed trade in this league with retroactive grades for each side. "
        "Grade scale uses community values: A+ = +1500 community gain · A = +800 · "
        "B = +250 · C = ±250 push · D = −250 to −800 · F = worse than −1500. "
        "Picks are valued by the drafted player when the pick has been used, by pick-model "
        "defaults otherwise."
    )
    if trades_history:
        trade_grades = grade_trades(
            trades_history, valued=valued_all, pick_model=pick_model, state=state,
        )
        history_rows = to_graded_rows(
            trades_history, roster_name, trade_grades,
            player_name_fn=player_name_fn,
        )
        st.dataframe(
            history_rows, hide_index=True, width="stretch",
            height=min(540, 50 + 36 * min(len(history_rows), 15)),
            column_config={
                "When": st.column_config.TextColumn("When", width="small"),
                "A grade": st.column_config.TextColumn(
                    "A grade", help="Side A's grade. A+/A = clear win, B = small win, "
                                    "C = roughly even, D/F = loss in community value.",
                    width="small",
                ),
                "A Δ": st.column_config.NumberColumn(
                    "A Δ", help="Side A's net community-value delta (received − given). "
                                "Positive = won value.",
                    format="%+d", width="small",
                ),
                "B grade": st.column_config.TextColumn(
                    "B grade", help="Side B's grade — mirror of A's.", width="small",
                ),
                "B Δ": st.column_config.NumberColumn(
                    "B Δ", help="Side B's net community-value delta.",
                    format="%+d", width="small",
                ),
            },
        )

        # Quick distribution summary so the user gets a feel for how grades pan out
        from collections import Counter
        all_grades = Counter()
        for sides in trade_grades.values():
            for g in sides.values():
                all_grades[g.grade] += 1
        if all_grades:
            grade_order = ["A+", "A", "B", "C", "D", "F"]
            summary = "  ·  ".join(
                f"{g}: {all_grades.get(g, 0)}" for g in grade_order if all_grades.get(g, 0)
            )
            st.caption(f"Distribution across all sides: {summary}")
    else:
        st.info("No completed trades found yet in this league's transaction log.")


# ------------------------------ Tab: Why? -------------------------------------

with tab_explain:
    st.subheader("Peek under the hood — full ranking pipeline for any player")
    st.caption(
        "Five stages: per-stat projection → league-adjusted points → dynasty curve (age + strategy) → "
        "replacement-level VBD → ranks. Each is shown below for whichever player you pick."
    )

    # Search box → filter the player list → selectbox shows matches
    search_col, _ = st.columns([2, 3])
    with search_col:
        whysearch = st.text_input(
            "Search player",
            placeholder="Start typing — e.g. dart, mahomes, bowers",
            label_visibility="collapsed",
        )
    q = whysearch.strip().lower()
    if q:
        matches = [v for v in valued_all if q in v.name.lower()][:50]
    else:
        matches = valued_all[:50]  # default: top 50 by Dyn value

    if not matches:
        st.warning(f"No players matched '{whysearch}'.")
        st.stop()

    selected = st.selectbox(
        "Match",
        matches,
        format_func=lambda v: f"{v.name}  ·  {v.position} {v.team or 'FA'}  ·  {v.age or '?'}y  ·  Dyn {fmt_ba(v.pct_dynasty)}",
        label_visibility="collapsed",
    )
    pid = selected.player_id
    proj = projections[pid]
    vp = selected

    st.markdown(
        f"### {proj.name} — {pos_chip(proj.position)} · {proj.team or 'FA'} · {proj.age or '?'}y · "
        f"<span style='color:#94a3b8'>overall #{vp.overall_rank} · "
        f"{proj.position}#{vp.position_rank}</span>",
        unsafe_allow_html=True,
    )

    # Top KPI strip — common-sized batting averages + raw values side by side
    col_a, col_b, col_c, col_d, col_e = st.columns(5)
    col_a.metric("Season pts", f"{proj.league_points:.0f}",
                 delta=f"{proj.league_points - proj.sleeper_pts_ppr:+.0f} vs default",
                 help="Projected fantasy points this season under YOUR league's exact scoring_settings.")
    col_b.metric("Dyn", fmt_ba(vp.pct_dynasty),
                 help=f"Dynasty value rank (raw {vp.dynasty_value:.0f}). 1.000 = top of pool.")
    col_c.metric("Comm", fmt_ba(vp.pct_community) if vp.pct_community is not None else "—",
                 help=f"Community-value rank (raw {(vp.ktc_value or 0):.0f}).")
    col_d.metric("Adj Comm", fmt_ba(vp.pct_adj_community) if vp.pct_adj_community is not None else "—",
                 help="Community value × your league-fit multiplier.")
    col_e.metric("VBD", fmt_ba(vp.pct_vbd),
                 help=f"Value over replacement (raw {vp.replacement_delta:+.0f}).")

    # ------ Stage 1 + 2: projection × scoring -----------------------------
    st.divider()
    st.markdown("##### Stage 1 + 2 — Per-stat projection × your league scoring")
    st.caption(
        f"Source: Sleeper bulk projections (provider: {proj.company or 'rotowire'}). "
        f"Last updated: {(datetime.fromtimestamp(proj.last_modified_ms/1000).strftime('%b %d') if proj.last_modified_ms else '?')}. "
        "We multiply each stat by your scoring_settings and sum."
    )
    all_comps = explain_scoring_components(proj.stats, cfg, top_n=20)
    score_rows = [
        {
            "Stat": stat,
            "Value": f"{proj.stats.get(stat, 0):.1f}",
            "Multiplier": f"×{cfg.scoring.get(stat, 0):+g}",
            "Points": round(pts, 2),
        }
        for stat, pts in all_comps
    ]
    # Add a TOTAL row so the math is explicit
    score_rows.append({"Stat": "— TOTAL —", "Value": "", "Multiplier": "", "Points": round(proj.league_points, 2)})
    st.dataframe(score_rows, hide_index=True, width="stretch")

    # ------ Stage 3: dynasty curve ----------------------------------------
    st.divider()
    st.markdown("##### Stage 3 — Dynasty curve (age decay × strategy weight)")
    st.caption(
        f"Strategy: **{cfg.strategy}** · Horizon: **{cfg.age_horizon_years} years**. "
        "Each future year is multiplied by an age-curve factor (position-specific) and a "
        "strategy weight (compete frontloads, rebuild backloads)."
    )
    breakdown = dynasty_value_breakdown(
        season_points=proj.league_points,
        position=proj.position,
        age=proj.age,
        strategy=cfg.strategy,
        horizon_years=cfg.age_horizon_years,
    )
    breakdown_rows = [
        {
            "Year": f"Y{r['year']} (age {r['age']})" if r['age'] is not None else f"Y{r['year']}",
            "Age multiplier": r["age_mult"],
            "Strategy weight": r["strategy_weight"],
            "Contribution (pts)": r["points"],
        }
        for r in breakdown
    ]
    breakdown_rows.append({
        "Year": "— TOTAL = dynasty value —",
        "Age multiplier": "",
        "Strategy weight": "",
        "Contribution (pts)": round(sum(r["points"] for r in breakdown), 2),
    })
    st.dataframe(breakdown_rows, hide_index=True, width="stretch")

    # ------ Stage 4: VBD context ------------------------------------------
    st.divider()
    st.markdown("##### Stage 4 — VBD (value over replacement)")
    # Compute the replacement player at this position
    same_pos_valued = [v for v in valued_all if v.position == proj.position]
    from src.valuation import scaled_replacement_ranks  # local import: not on module hot path
    reps = scaled_replacement_ranks(cfg.teams)
    if cfg.superflex:
        reps["QB"] = max(reps["QB"], 24)
    rep_idx = min(reps.get(proj.position, len(same_pos_valued)), len(same_pos_valued)) - 1
    rep_player = same_pos_valued[rep_idx] if rep_idx >= 0 else None

    vbd_a, vbd_b, vbd_c = st.columns(3)
    if vp:
        vbd_a.metric("Your value", f"{vp.dynasty_value:.0f}")
    if rep_player:
        vbd_b.metric(
            f"{proj.position} replacement (#{rep_idx + 1})",
            f"{rep_player.dynasty_value:.0f}",
            help=f"{rep_player.name} — the player whose value sets the replacement floor in this SF/dynasty league.",
        )
    if vp and rep_player:
        vbd_c.metric("VBD (delta)", f"{vp.replacement_delta:+.0f}")

    # ------ Stage 5: final ranks ------------------------------------------
    st.divider()
    st.markdown("##### Stage 5 — Final ranks")
    if vp:
        r1, r2, r3 = st.columns(3)
        r1.metric("Overall rank", f"#{vp.overall_rank}")
        r2.metric(f"{proj.position} rank", f"#{vp.position_rank}")
        r3.metric("ID match method", vp.match_method,
                  help="How we linked this player to DynastyProcess data. 'sleeper_id' is the canonical bridge; "
                       "'fuzzy_name' fell back to name matching; 'unmatched' means no DP cross-walk found.")
