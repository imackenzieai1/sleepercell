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
from src.trade_recommender import recommend_offers
from src.transaction_history import (
    activity_per_roster,
    parse_trades,
    to_normalized_rows,
)
from src.projections import (
    build_projection_index,
    explain_scoring_components,
    points_per_game,
)
from src.simulator import build_consensus_index, simulate_next_picks
from src.sleeper_client import SleeperClient, SleeperError
from src.team_analysis import league_needs, positional_scarcity, team_summary
from src.tier_engine import detect_tiers, tier_cliff_alerts
from src.valuation import filter_undrafted, value_population
from src.vbd import VBDContext, best_available_dynamic, likely_next_available_value


# --------------------------------------------------------------- Setup

st.set_page_config(page_title="Sleeper Cell", layout="wide", initial_sidebar_state="expanded")

# ---------------------------------------------------------------- Global stylesheet
# One block of CSS keeps the look consistent: data-forward, tight, no decoration.
st.markdown("""
<style>
:root {
  --bg-page: #ffffff;
  --bg-soft: #f8fafc;
  --bg-sidebar: #fafbfc;
  --border: #e5e7eb;
  --border-soft: #f1f5f9;
  --ink: #0f172a;
  --ink-2: #334155;
  --muted: #64748b;
  --muted-2: #94a3b8;
  --accent: #0f172a;
  --good: #059669;
  --warn: #d97706;
  --bad:  #dc2626;
}
.block-container {
  padding-top: 1.25rem !important;
  padding-bottom: 2rem !important;
  max-width: 1500px;
}
[data-testid="stHeader"] { background: transparent; height: 0; }
html, body, [class*="css"] {
  font-family: ui-sans-serif, system-ui, -apple-system, "Inter", "Segoe UI", Roboto, sans-serif;
  font-feature-settings: "tnum" 1, "ss01" 1;
}
h1 { font-size: 1.35rem !important; font-weight: 700 !important; letter-spacing: -0.01em; margin: 0 0 0.15rem 0 !important; color: var(--ink); }
h2 { font-size: 1rem !important; font-weight: 600 !important; margin-top: 1.2rem !important; margin-bottom: 0.4rem !important; letter-spacing: -0.005em; color: var(--ink); }
h3, .stMarkdown h3 { font-size: 0.72rem !important; font-weight: 700 !important; color: var(--muted) !important; text-transform: uppercase !important; letter-spacing: 0.08em !important; margin-top: 0.8rem !important; margin-bottom: 0.4rem !important; }
.stMarkdown h4, h4 { font-size: 0.85rem !important; font-weight: 600 !important; color: var(--ink) !important; margin-top: 0.5rem !important; margin-bottom: 0.3rem !important; }
.stMarkdown h5, h5 { font-size: 0.78rem !important; font-weight: 600 !important; color: var(--ink-2) !important; text-transform: uppercase !important; letter-spacing: 0.06em !important; margin-top: 0.8rem !important; margin-bottom: 0.3rem !important; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 0 !important; border-bottom: 1px solid var(--border) !important; margin-bottom: 1rem !important; }
.stTabs [data-baseweb="tab"] { padding: 0.45rem 0.9rem !important; font-size: 0.82rem !important; font-weight: 600 !important; color: var(--muted) !important; }
.stTabs [aria-selected="true"] { color: var(--ink) !important; border-bottom: 2px solid var(--ink) !important; }
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }

/* Sidebar */
[data-testid="stSidebar"] { background: var(--bg-sidebar); border-right: 1px solid var(--border); }
[data-testid="stSidebar"] .stMarkdown h2 { font-size: 0.7rem !important; color: var(--muted-2) !important; text-transform: uppercase; letter-spacing: 0.1em; margin-top: 1rem !important; }
[data-testid="stSidebar"] label p { font-size: 0.82rem; }
[data-testid="stSidebar"] .stRadio label { font-size: 0.78rem !important; }

/* Metric tiles */
[data-testid="stMetric"] { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem 0.7rem; }
[data-testid="stMetricLabel"] { font-size: 0.65rem !important; color: var(--muted) !important; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
[data-testid="stMetricValue"] { font-size: 1.25rem !important; font-weight: 700 !important; color: var(--ink) !important; font-variant-numeric: tabular-nums; }
[data-testid="stMetricDelta"] { font-size: 0.7rem !important; }

/* DataFrames */
[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 4px; }

/* Captions */
[data-testid="stCaptionContainer"] p, .caption { color: var(--muted) !important; font-size: 0.72rem !important; font-style: normal !important; line-height: 1.45; }

/* Code blocks */
pre code { font-size: 0.78rem !important; font-family: ui-monospace, "SF Mono", Menlo, monospace !important; }

/* Dividers */
hr { margin: 0.75rem 0 !important; border: none !important; border-top: 1px solid var(--border) !important; }

/* Position tag */
.tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  font-family: ui-sans-serif, system-ui, sans-serif;
  vertical-align: middle;
}
.tag-qb { background: #2563eb; color: white; }
.tag-rb { background: #059669; color: white; }
.tag-wr { background: #dc2626; color: white; }
.tag-te { background: #d97706; color: white; }
.tag-flat { background: var(--bg-soft); color: var(--ink-2); border: 1px solid var(--border); }

/* Section header row with right-aligned meta */
.sec-head {
  display: flex; justify-content: space-between; align-items: baseline;
  margin: 1rem 0 0.35rem 0;
}
.sec-head h2 { margin: 0 !important; }
.sec-head .meta { color: var(--muted); font-size: 0.72rem; }

/* Verdict banner */
.verdict {
  border-left: 4px solid var(--accent);
  background: var(--bg-soft);
  padding: 0.6rem 0.9rem;
  margin: 0.5rem 0 1rem 0;
  border-radius: 0 3px 3px 0;
}
.verdict .label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); font-weight: 700; }
.verdict .text { font-size: 1rem; font-weight: 700; color: var(--ink); margin-top: 2px; }
.verdict .sub  { font-size: 0.75rem; color: var(--muted); margin-top: 4px; font-variant-numeric: tabular-nums; }
.verdict.send   { border-left-color: var(--good); }
.verdict.send  .text { color: var(--good); }
.verdict.offer  { border-left-color: var(--warn); }
.verdict.offer .text { color: var(--warn); }
.verdict.pass   { border-left-color: var(--bad); }
.verdict.pass  .text { color: var(--bad); }

/* Plain "label : value" inline rows */
.kv { font-size: 0.78rem; color: var(--ink-2); margin: 0.15rem 0; }
.kv span.k { color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.62rem; margin-right: 8px; }
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

    default_league = st.secrets.get("sleeper", {}).get("league_id") if hasattr(st, "secrets") else None
    default_draft = st.secrets.get("sleeper", {}).get("draft_id") if hasattr(st, "secrets") else None

    st.markdown("## Identifiers")
    league_id = st.text_input("League ID", value=default_league or "")
    draft_id = st.text_input("Draft ID", value=default_draft or "")

    st.markdown("## Strategy")
    strategy = st.radio(
        "Strategy", ["compete", "balanced", "rebuild"], index=0, horizontal=True,
        label_visibility="collapsed",
        help="Compete weights near-term production. Rebuild discounts year 1.",
    )
    horizon = st.slider("Dynasty horizon (years)", min_value=2, max_value=8, value=4)

    st.markdown("## Bylaws")
    bylaw_3rr = st.checkbox("Third Round Reversal", value=True)

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
valued_all = value_population(projections, index, cfg)
drafted_ids = state.drafted_ids()
valued_undrafted = filter_undrafted(valued_all, drafted_ids)

# Identity: pick "you" by trying to find the current user. For Trade Whores hardcode roster 11 if matches.
my_roster_id = st.session_state.get("my_roster_id")
if not my_roster_id:
    # Try matching session user via Sleeper account name in secrets, else default to slot 12 if exists
    my_user_id = st.secrets.get("sleeper", {}).get("user_id") if hasattr(st, "secrets") else None
    if my_user_id:
        for r in rosters:
            if str(r.get("owner_id")) == str(my_user_id):
                my_roster_id = r["roster_id"]
                break
    if not my_roster_id and rosters:
        my_roster_id = rosters[0]["roster_id"]

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

tab_board, tab_best, tab_team, tab_tiers, tab_trade, tab_explain = st.tabs([
    "Draft Board", "Best Available", "My Team", "Tier Alerts", "Trade Targets", "Why?",
])


# ------------------------------ Tab: Draft Board ------------------------------

with tab_board:
    # ----- Recent picks (clean table) -----
    st.markdown('<div class="sec-head"><h2>Recent picks</h2>'
                f'<div class="meta">{len([sp for sp in state.schedule if sp.made])} of {len(state.schedule)} made</div></div>',
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
    dynamic_top = best_available_dynamic(vctx, top_n=25)

    st.markdown(
        f'<div class="sec-head"><h2>Best available</h2>'
        f'<div class="meta">strategy: {strategy} · horizon: {horizon}y · {picks_until_mine} picks until you</div></div>',
        unsafe_allow_html=True,
    )
    pos_filter = st.multiselect("Position", ["QB", "RB", "WR", "TE"], default=["QB", "RB", "WR", "TE"],
                                label_visibility="collapsed")

    filtered = [v for v in dynamic_top if v.position in pos_filter][:20]
    likely = likely_next_available_value(vctx)
    rows = []
    for v in filtered:
        delta_next = round(v.dynasty_value - likely.get(v.position, 0.0), 1)
        rows.append({
            "Rank": v.overall_rank,
            "Pos": v.position,
            "Name": v.name,
            "Team": v.team or "FA",
            "Age": v.age or "—",
            "Season pts (TW scoring)": v.season_points,
            "Dynasty value": v.dynasty_value,
            "VBD": v.replacement_delta,
            "Dynamic VBD (wait?)": delta_next,
        })
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("**Dynamic VBD** = dynasty_value − value of the player likely available at your next pick. "
               "Positive = take now. Negative = you can probably wait at that position.")

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
    tiers = detect_tiers(valued_all)
    alerts = tier_cliff_alerts(tiers, drafted_ids)

    st.markdown('<div class="sec-head"><h2>Tier cliff alerts</h2>'
                f'<div class="meta">{len(alerts)} active</div></div>',
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

    st.markdown('<div class="sec-head"><h2>Tiers by position</h2></div>', unsafe_allow_html=True)
    pcol = st.columns(4)
    for i, pos in enumerate(["QB", "RB", "WR", "TE"]):
        with pcol[i]:
            st.markdown(f"##### {pos}", unsafe_allow_html=True)
            for tier in tiers.get(pos, [])[:6]:
                undrafted_in_tier = [p for p in tier.players if p.player_id not in drafted_ids]
                lines = []
                for p in tier.players[:8]:
                    drafted = p.player_id in drafted_ids
                    mark = '<span style="color:#94a3b8;text-decoration:line-through">' if drafted else '<span>'
                    lines.append(
                        f'{mark}{p.name}</span> '
                        f'<span style="color:var(--muted-2);font-variant-numeric:tabular-nums">{p.dynasty_value:.0f}</span>'
                    )
                st.markdown(
                    f'<div style="font-size:0.75rem;margin-bottom:0.6rem;">'
                    f'<div style="font-weight:600;color:var(--ink-2);margin-bottom:2px">'
                    f'T{tier.tier_number} <span style="color:var(--muted);font-weight:500">'
                    f'· {len(undrafted_in_tier)}/{tier.size} left</span></div>'
                    + "<br>".join(lines) +
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

    # ---------- RECOMMENDED OFFERS ----------
    st.markdown('<div class="sec-head"><h2>Recommended offers</h2>'
                '<div class="meta">auto-generated · mutual-win scored</div></div>',
                unsafe_allow_html=True)
    st.caption(
        "Scans every partner and every plausible asset combination. Ranks by mutual-win "
        "(your TW gain × their consensus gain). Only shows offers where you net positive in TW "
        "and the partner is likely to accept or counter."
    )

    # Parse transaction history for trade-activity weighting
    player_name_fn = lambda pid: (projections[pid].name if pid in projections else f"player {pid}")
    trades_history = parse_trades(transactions_raw, roster_name_fn=roster_name)
    activity_counts = activity_per_roster(trades_history)

    if pick_model:
        offers = recommend_offers(
            state=state, valued_all=valued_all, projections=projections,
            pick_model=pick_model, cfg=cfg, my_roster_id=my_roster_id,
            roster_name_fn=roster_name,
            activity_per_roster=activity_counts,
        )
    else:
        offers = []

    if not offers:
        st.info("No high-confidence offers right now. Try again after the next few picks.")
    else:
        # Compact summary table first
        offer_table = []
        for i, o in enumerate(offers, start=1):
            offer_table.append({
                "#": i,
                "Partner": o.partner_name,
                "You give": ", ".join((a.name if isinstance(a, PlayerAsset) else a.label) for a in o.give),
                "You get":  ", ".join((a.name if isinstance(a, PlayerAsset) else a.label) for a in o.get),
                "TW gain": round(o.eval.tw_delta),
                "Their consensus gain": round(o.eval.consensus_delta_for_them),
                "Verdict": o.eval.combined,
            })
        st.dataframe(offer_table, hide_index=True, width="stretch",
                     height=min(540, 50 + 36 * len(offer_table)))

        # Detailed expandable cards for each offer
        st.markdown('<h5>Offer detail</h5>', unsafe_allow_html=True)
        for i, o in enumerate(offers, start=1):
            verdict_class = "send" if o.eval.combined in ("SEND IT", "SEND") else (
                "offer" if o.eval.combined.startswith("OFFER") or "FAIR" in o.eval.combined else "pass"
            )
            head = f"#{i} → {o.partner_name} · {o.eval.combined} · TW {o.eval.tw_delta:+.0f}"
            with st.expander(head, expanded=(i == 1)):
                c_give, c_get, c_kpi = st.columns([2, 2, 1])
                with c_give:
                    st.markdown("**You give**")
                    for a in o.give:
                        if isinstance(a, PlayerAsset):
                            st.markdown(
                                f"&middot; {pos_chip(a.position)} **{a.name}** "
                                f"<span style='color:var(--muted);font-size:0.72rem'>"
                                f"TW {a.tw_value:.0f} · cons {a.consensus_value:.0f}</span>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                f"&middot; {a.label} "
                                f"<span style='color:var(--muted);font-size:0.72rem'>"
                                f"TW {a.tw_value:.0f} · cons {a.consensus_value:.0f}</span>",
                                unsafe_allow_html=True,
                            )
                with c_get:
                    st.markdown(f"**You get** (from {o.partner_name})")
                    for a in o.get:
                        if isinstance(a, PlayerAsset):
                            st.markdown(
                                f"&middot; {pos_chip(a.position)} **{a.name}** "
                                f"<span style='color:var(--muted);font-size:0.72rem'>"
                                f"TW {a.tw_value:.0f} · cons {a.consensus_value:.0f}</span>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                f"&middot; {a.label} "
                                f"<span style='color:var(--muted);font-size:0.72rem'>"
                                f"TW {a.tw_value:.0f} · cons {a.consensus_value:.0f}</span>",
                                unsafe_allow_html=True,
                            )
                with c_kpi:
                    st.metric("Your TW", f"{o.eval.tw_delta:+.0f}")
                    st.metric("Their cons.", f"{o.eval.consensus_delta_for_them:+.0f}")

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
            f'<div class="sub">Your view (TW): {result.tw_delta:+.0f} &middot; '
            f'Their view (consensus): {result.consensus_delta_for_them:+.0f} &middot; '
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
                f'<div class="meta">{len(trades_history)} completed trades</div></div>',
                unsafe_allow_html=True)
    st.caption(
        "Every completed trade in this league — startup picks, future picks, "
        "players, FAAB. The recommender uses these counts to weight which "
        "partners are more likely to deal again."
    )
    if trades_history:
        history_rows = to_normalized_rows(trades_history, roster_name, player_name_fn=player_name_fn)
        st.dataframe(history_rows, hide_index=True, width="stretch",
                     height=min(540, 50 + 36 * min(len(history_rows), 15)))
    else:
        st.info("No completed trades found yet in this league's transaction log.")


# ------------------------------ Tab: Why? -------------------------------------

with tab_explain:
    st.subheader("Peek under the hood — full ranking pipeline for any player")
    st.caption(
        "Five stages: per-stat projection → league-adjusted points → dynasty curve (age + strategy) → "
        "replacement-level VBD → ranks. Each is shown below for whichever player you pick."
    )

    options = [(v.name, v.player_id, v.season_points, v.dynasty_value, v.overall_rank) for v in valued_all[:300]]
    choice = st.selectbox(
        "Pick a player",
        options,
        format_func=lambda x: f"#{x[4]:>3} · {x[0]} — {x[2]:.0f} season pts, {x[3]:.0f} dyn value",
    )
    if choice:
        pid = choice[1]
        proj = projections[pid]
        vp = next((v for v in valued_all if v.player_id == pid), None)

        st.markdown(
            f"### {proj.name} — {pos_chip(proj.position)} · {proj.team or 'FA'} · {proj.age or '?'}y · "
            f"<span style='color:#94a3b8'>overall #{vp.overall_rank if vp else '?'} · "
            f"{proj.position}#{vp.position_rank if vp else '?'}</span>",
            unsafe_allow_html=True,
        )

        col_metrics_a, col_metrics_b, col_metrics_c, col_metrics_d = st.columns(4)
        col_metrics_a.metric("Season pts (TW)", f"{proj.league_points:.0f}")
        col_metrics_b.metric("Sleeper default ppr", f"{proj.sleeper_pts_ppr:.0f}",
                             delta=f"{proj.league_points - proj.sleeper_pts_ppr:+.0f} vs default")
        col_metrics_c.metric("Dynasty value", f"{vp.dynasty_value:.0f}" if vp else "—")
        col_metrics_d.metric(
            "DP Superflex (ref)",
            f"{vp.dp_value_2qb:.0f}" if (vp and vp.dp_value_2qb) else "—",
            help="DynastyProcess generic SF value, for sanity-check. Different units than ours — compare order, not absolute.",
        )

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
