from __future__ import annotations

import streamlit as st


def ensure_state() -> None:
    st.session_state.setdefault("selected_run_dir", "")
    st.session_state.setdefault("quiz_started", False)
    st.session_state.setdefault("quiz_index", 0)
    st.session_state.setdefault("quiz_answers", {})  # idx -> answer payload (type-specific)
    st.session_state.setdefault("quiz_submitted", set()) # set of indices
    st.session_state.setdefault("quiz_seed", 0)


def reset_quiz(seed: int) -> None:
    st.session_state.quiz_started = True
    st.session_state.quiz_index = 0
    st.session_state.quiz_answers = {}
    st.session_state.quiz_submitted = set()
    st.session_state.quiz_seed = seed


def stop_quiz() -> None:
    st.session_state.quiz_started = False
    st.session_state.quiz_index = 0
    st.session_state.quiz_answers = {}
    st.session_state.quiz_submitted = set()
