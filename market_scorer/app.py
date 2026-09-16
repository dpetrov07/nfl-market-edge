"""Small read-only UI for live shadow scoring and DAL-NYG replay."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from live import ROOT, combine, live_snapshot, model_predict
from model import HOLDOUT_GAME, build_rows


V1 = ROOT / "model_output/player_prop_markout_v1/model.json"
V2 = ROOT / "model_output/player_prop_markout_v2/model.json"
SUNDAY = ROOT / "data/sunday_2026-09-13"


@st.cache_data(ttl=15, show_spinner=False)
def fetch_live(game, game_date):
    return live_snapshot(game, game_date, V2)


@st.cache_data(show_spinner=False)
def replay_rows():
    model = json.loads(V1.read_text())
    rows = build_rows(
        SUNDAY,
        games={HOLDOUT_GAME},
        require_future_isolation=False,
    )
    for row in rows:
        prediction = model_predict(model, row)
        combined, basis = combine(row["recommendation"], prediction)
        row.update({
            "model_predicted_markout_10s": prediction,
            "combined_recommendation": combined,
            "combined_basis": basis,
        })
    return rows


def table(rows, live=False, reveal=False):
    data = []
    for row in rows:
        data.append({
            "Player": row["player"],
            "Prop": "Receiving" if row["prop_type"] == "receiving_yards" else "Rushing",
            "Line / side": f'{row["threshold"]:g} {row["entry_side"].upper()}',
            "Kalshi price": row["executable_price"] if live else row["kalshi_executable_price"],
            "Bovada fair %": row["bovada_fair_probability"] * 100,
            "Residual (¢)": row["gross_disagreement"] * 100,
            "Spread (¢)": (row["spread"] if live else row["kalshi_spread"]) * 100,
            "Size": row["available_size"] if live else row["kalshi_available_size"],
            "Rule tier": row["rule_tier"] if live else row["recommendation"],
            "Model +10s (¢)": row["model_predicted_markout_10s"] * 100,
            "Combined": row["combined_recommendation"],
            **({
                "Actual +10s (¢)": row["historical_markout_10s"] * 100,
                "Actual +30s (¢)": row["historical_markout_30s"] * 100,
            } if reveal else {}),
        })
    return pd.DataFrame(data)


def show_table(frame):
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={
            "Kalshi price": st.column_config.NumberColumn(format="%.2f"),
            "Bovada fair %": st.column_config.NumberColumn(format="%.1f%%"),
            "Residual (¢)": st.column_config.NumberColumn(format="%+.1f"),
            "Spread (¢)": st.column_config.NumberColumn(format="%.1f"),
            "Size": st.column_config.NumberColumn(format="%.0f"),
            "Model +10s (¢)": st.column_config.NumberColumn(format="%+.1f"),
            "Actual +10s (¢)": st.column_config.NumberColumn(format="%+.1f"),
            "Actual +30s (¢)": st.column_config.NumberColumn(format="%+.1f"),
        },
    )


def live_page():
    st.caption("Read-only shadow mode. Exact player/prop/threshold matches only. No execution.")
    left, right, refresh = st.columns([2, 2, 1])
    game = left.text_input("Game", "DET_BUF").strip().upper()
    game_date = right.date_input("Game date", date.today() + timedelta(days=1))
    if refresh.button("Refresh", width="stretch"):
        fetch_live.clear()
    try:
        with st.spinner("Fetching Kalshi and Bovada…"):
            result = fetch_live(game, game_date.isoformat())
    except Exception as exc:
        st.error(str(exc))
        return

    verification = result["verification"]
    a, b, c = st.columns(3)
    a.metric("Exact matches", verification["exact_player_prop_threshold_matches"])
    b.metric("Executable Kalshi quotes", verification["kalshi_executable_quotes"])
    c.metric("Timestamp skew", f'{result["timestamp_skew_seconds"]:.2f}s')
    st.caption(
        f'{result["game"]} · Kalshi {result["kalshi_timestamp"]} · '
        f'Bovada {result["bovada_timestamp"]}'
    )
    if not result["opportunities"]:
        st.warning("No exact two-sided Bovada ↔ Kalshi threshold matches right now.")
        return
    show_table(table(result["opportunities"], live=True))
    st.caption(
        "Initial snapshots have no Bovada repricing context, so the rule tier is "
        "limited to watch/pass; model-only promotions remain watch."
    )


def replay_page():
    st.caption(
        "Historical sanity replay using frozen V1. Future markouts are hidden and "
        "never enter rule/model features. This is not a new unbiased test."
    )
    with st.spinner("Loading DAL–NYG capture…"):
        rows = replay_rows()
    timestamps = sorted({row["bovada_repriced_at"] for row in rows})
    selected = st.select_slider(
        "Decision timestamp",
        options=timestamps,
        value=timestamps[-1],
        format_func=lambda value: value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
    )
    current = [row for row in rows if row["bovada_repriced_at"] == selected]
    key = selected.isoformat()
    if st.button("Reveal actual +10s / +30s markout"):
        st.session_state["revealed_replay_timestamp"] = key
    reveal = st.session_state.get("revealed_replay_timestamp") == key
    show_table(table(current, reveal=reveal))
    st.caption(
        "Replay event selection uses prior information only; the original future "
        "12-second isolation check is disabled."
    )


st.set_page_config(page_title="NFL Prop Shadow Scorer", layout="wide")
st.title("NFL Prop Shadow Scorer")
mode = st.radio("Mode", ("Live", "DAL–NYG Replay"), horizontal=True)
live_page() if mode == "Live" else replay_page()
