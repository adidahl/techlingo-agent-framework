from __future__ import annotations

from typing import Any, Optional, cast

import streamlit as st
from techlingo_workflow.models import (
    Feedback,
    FillGapsExercise,
    FillGapsGapPart,
    FillGapsTextPart,
    MultiChoiceExercise,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
)

from .utils import accepted_match, choice_options_for_exercise


def render_exercise_browse(ex: Any) -> None:
    st.markdown(f"**Type:** `{getattr(ex, 'question_type', 'unknown')}`")
    st.markdown(f"**Prompt:** {getattr(ex, 'prompt', '')}")

    if isinstance(ex, (SingleChoiceExercise, MultiChoiceExercise)):
        st.markdown("**Options:**")
        for o in ex.options:
            flag = "✅" if o.is_correct else "❌"
            st.markdown(f"- {flag} {o.text}")
            if o.feedback and isinstance(o.feedback, Feedback):
                st.markdown(f"  - intrinsic: {o.feedback.intrinsic}")
                st.markdown(f"  - instructional: {o.feedback.instructional}")

    elif isinstance(ex, TrueFalseExercise):
        st.markdown(f"**Statement:** {ex.statement}")
        st.markdown(f"**Correct:** {'True' if ex.correct_answer else 'False'}")
        if ex.feedback_for_incorrect and isinstance(ex.feedback_for_incorrect, Feedback):
            st.markdown("**Feedback for incorrect:**")
            st.markdown(f"- intrinsic: {ex.feedback_for_incorrect.intrinsic}")
            st.markdown(f"- instructional: {ex.feedback_for_incorrect.instructional}")

    elif isinstance(ex, FillGapsExercise):
        st.markdown("**Fill-gaps preview:**")
        preview = []
        gap_i = 0
        for p in ex.parts:
            if isinstance(p, FillGapsTextPart):
                preview.append(p.text)
            else:
                preview.append(f"____({gap_i+1})____")
                gap_i += 1
        st.code("".join(preview))
        st.caption(f"Gaps: {gap_i}")

    elif isinstance(ex, RearrangeExercise):
        st.markdown("**Word bank:** " + ", ".join(ex.word_bank))
        st.markdown("**Correct order:** " + " | ".join(ex.correct_order))

    else:
        st.caption("Unsupported exercise type for browse view.")


def render_feedback_block(is_correct: bool, correct_feedback: Optional[str], incorrect_feedback: Optional[Feedback|str], rationale: Optional[str] = None, correct_answer_label: Optional[str] = None, correct_answer_rationale: Optional[str] = None):
    if is_correct:
        st.success("Correct! ✅")
        if correct_feedback:
            st.markdown(f"**Feedback:** {correct_feedback}")
    else:
        st.error("Incorrect ❌")
        if incorrect_feedback:
            if isinstance(incorrect_feedback, Feedback):
                st.markdown(f"**Intrinsic Feedback:** {incorrect_feedback.intrinsic}")
                st.markdown(f"**Instructional Feedback:** {incorrect_feedback.instructional}")
            else:
                st.markdown(f"**Feedback:** {incorrect_feedback}")

    if rationale:
        st.info(f"**Rationale:** {rationale}")

    if not is_correct and correct_answer_label:
        st.markdown("---")
        st.markdown(f"**Correct Answer:** {correct_answer_label}")
        if correct_answer_rationale:
             st.markdown(f"**Rationale:** {correct_answer_rationale}")


def render_exercise_quiz(ex: Any, *, idx: int, seed: int) -> None:
    key = str(idx)
    saved = st.session_state.quiz_answers.get(key)
    submitted = idx in st.session_state.quiz_submitted
    
    # Common container for inputs
    container = st.container()

    # Determine correctness and gather feedback info
    is_correct = False
    correct_feedback = getattr(ex, "feedback_for_correct", None)
    incorrect_feedback = None
    rationale = None
    correct_answer_label = None
    correct_answer_rationale = None

    # Logic to capture input and determine state
    with container:
        if isinstance(ex, SingleChoiceExercise):
            options = choice_options_for_exercise(ex, seed=seed)
            labels = [o.label for o in options]
            id_by_label = {o.label: o.id for o in options}
            opt_by_id = {o.id: o for o in options}

            prev_id = cast(Optional[str], saved) if isinstance(saved, str) else None
            prev_label = next((o.label for o in options if o.id == prev_id), None)
            default_index = labels.index(prev_label) if prev_label in labels else 0
            
            choice_label = st.radio("Choose one", labels, index=default_index, disabled=submitted)
            if not submitted:
                choice_id = id_by_label[choice_label]
                st.session_state.quiz_answers[key] = choice_id
            
            # If submitted, calculate feedback
            if submitted and prev_id:
                chosen = opt_by_id[prev_id]
                is_correct = chosen.is_correct
                incorrect_feedback = chosen.feedback
                rationale = chosen.rationale
                
                correct_opt = next((o for o in options if o.is_correct), None)
                if correct_opt:
                    correct_answer_label = correct_opt.label
                    correct_answer_rationale = correct_opt.rationale

        elif isinstance(ex, MultiChoiceExercise):
            options = choice_options_for_exercise(ex, seed=seed)
            labels = [o.label for o in options]
            id_by_label = {o.label: o.id for o in options}
            opt_by_id = {o.id: o for o in options}

            prev_ids = saved if isinstance(saved, list) else []
            prev_labels = [next((o.label for o in options if o.id == pid), None) for pid in prev_ids]
            
            picked_labels = st.multiselect("Choose all that apply", labels, default=prev_labels, disabled=submitted)
            if not submitted:
                 picked_ids = [id_by_label[l] for l in picked_labels]
                 st.session_state.quiz_answers[key] = picked_ids
            
            if submitted:
                 picked_ids = prev_ids
                 correct_ids = {o.id for o in options if o.is_correct}
                 picked_set = set(picked_ids)
                 is_correct = picked_set == correct_ids
                 
                 if not is_correct:
                    wrong = next((opt_by_id[i] for i in picked_ids if not opt_by_id[i].is_correct), None)
                    if wrong:
                        incorrect_feedback = wrong.feedback

        elif isinstance(ex, TrueFalseExercise):
            prev = saved if isinstance(saved, bool) else None
            options = ["True", "False"]
            default_idx = 0 if prev is True else 1 if prev is False else 0
            
            picked = st.radio("True or False?", options, index=default_idx, disabled=submitted)
            if not submitted:
                ans = picked == "True"
                st.session_state.quiz_answers[key] = ans
            
            if submitted and prev is not None:
                is_correct = prev == ex.correct_answer
                incorrect_feedback = ex.feedback_for_incorrect

        elif isinstance(ex, FillGapsExercise):
            gaps: list[FillGapsGapPart] = [p for p in ex.parts if isinstance(p, FillGapsGapPart)]
            prev_vals = saved if isinstance(saved, list) else [""] * len(gaps)
            
            st.markdown("Fill in the blanks:")
            preview = []
            gap_i = 0
            for p in ex.parts:
                if isinstance(p, FillGapsTextPart):
                    preview.append(p.text)
                else:
                    preview.append(f"____({gap_i+1})____")
                    gap_i += 1
            st.code("".join(preview))

            vals = []
            for gi, gap in enumerate(gaps):
                placeholder = gap.placeholder or ""
                val = st.text_input(f"Gap {gi+1}", value=prev_vals[gi] if gi < len(prev_vals) else "", placeholder=placeholder, disabled=submitted)
                vals.append(val)
            
            if not submitted:
                st.session_state.quiz_answers[key] = vals
            
            if submitted:
                is_correct = all(accepted_match(prev_vals[i], gaps[i].accepted_answers) for i in range(len(gaps)))
                if not is_correct:
                    correct_lbls = []
                    for gi, gap in enumerate(gaps):
                         correct_lbls.append(f"Gap {gi+1}: {', '.join(gap.accepted_answers)}")
                    correct_answer_label = "\n".join(correct_lbls)

        elif isinstance(ex, RearrangeExercise):
            prev_order = saved if isinstance(saved, list) else []
            st.markdown("Arrange the tokens into the correct order:")
            st.caption("Word bank: " + " | ".join(ex.word_bank))

            order = []
            for pi in range(len(ex.correct_order)):
                default_val = prev_order[pi] if pi < len(prev_order) and prev_order[pi] in ex.word_bank else ex.word_bank[0]
                picked = st.selectbox(
                    f"Position {pi+1}", 
                    ex.word_bank, 
                    index=ex.word_bank.index(default_val), 
                    key=f"rearr_{idx}_{pi}",
                    disabled=submitted
                )
                order.append(picked)
            
            if not submitted:
                st.session_state.quiz_answers[key] = order
            
            if submitted:
                 is_correct = prev_order == ex.correct_order
                 if not is_correct:
                     correct_answer_label = " | ".join(ex.correct_order)

    # Render Feedback if submitted
    if submitted:
        render_feedback_block(is_correct, correct_feedback, incorrect_feedback, rationale, correct_answer_label, correct_answer_rationale)

    # Action Buttons
    b_cols = st.columns([0.2, 0.2, 0.6])
    
    with b_cols[0]:
        if not submitted:
            # Check if answer is provided before enabling "Submit"?
            # For now, just enable it.
            if st.button("Answer", type="primary"):
                 st.session_state.quiz_submitted.add(idx)
                 st.rerun()
        else:
            if st.button("Continue", type="primary"):
                 st.session_state.quiz_index = idx + 1 # Move to next
                 st.rerun()

    with b_cols[1]:
        if submitted:
             if st.button("Reset"):
                 st.session_state.quiz_submitted.remove(idx)
                 st.rerun()
