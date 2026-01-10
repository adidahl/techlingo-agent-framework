from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

import streamlit as st

# Allow running without an editable install.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
import sys  # noqa: E402

sys.path.insert(0, str(_SRC))

from techlingo_workflow.models import (  # noqa: E402
    Course,
    Feedback,
    FillGapsExercise,
    FillGapsGapPart,
    FillGapsTextPart,
    MultiChoiceExercise,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
)


@dataclass(frozen=True)
class ChoiceUIOption:
    id: str
    label: str
    is_correct: bool
    feedback: Optional[Feedback]
    rationale: Optional[str] = None


def _list_runs(outputs_dir: Path) -> list[Path]:
    if not outputs_dir.exists():
        return []
    runs = sorted(outputs_dir.glob("run-*"), key=lambda p: p.name, reverse=True)
    return [p for p in runs if p.is_dir()]


def _load_course(run_dir: Path) -> Course:
    course_path = run_dir / "course.json"
    if not course_path.exists():
        raise FileNotFoundError(f"Missing course.json at {course_path}")
    data: dict[str, Any] = json.loads(course_path.read_text(encoding="utf-8"))
    data = _coerce_v1_to_v2(data)
    return Course.model_validate(data)


def _load_json_preview(path: Path, max_chars: int = 80_000) -> str:
    txt = path.read_text(encoding="utf-8", errors="replace")
    if len(txt) <= max_chars:
        return txt
    return txt[:max_chars] + "\n\n…(truncated)…\n"


def _flatten_exercises(course: Course) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for mi, mod in enumerate(course.modules):
        for li, lesson in enumerate(mod.lessons):
            for ei, ex in enumerate(lesson.exercises):
                flat.append(
                    {
                        "module_index": mi,
                        "lesson_index": li,
                        "exercise_index": ei,
                        "module_title": mod.title,
                        "lesson_title": lesson.title,
                        "slo": lesson.slo,
                        "blooms_level": ex.blooms_level.value,
                        "question_type": ex.question_type,
                        "exercise": ex,
                    }
                )
    return flat


def _choice_options_for_exercise(ex: SingleChoiceExercise | MultiChoiceExercise, *, seed: int) -> list[ChoiceUIOption]:
    opts: list[ChoiceUIOption] = []
    for oi, o in enumerate(ex.options):
        fb = o.feedback if isinstance(o.feedback, Feedback) else None
        opts.append(ChoiceUIOption(id=str(oi), label=o.text, is_correct=o.is_correct, feedback=fb, rationale=o.rationale))
    rnd = random.Random(seed)
    rnd.shuffle(opts)
    return opts


def _ensure_state() -> None:
    st.session_state.setdefault("selected_run_dir", "")
    st.session_state.setdefault("quiz_started", False)
    st.session_state.setdefault("quiz_index", 0)
    st.session_state.setdefault("quiz_answers", {})  # idx -> answer payload (type-specific)
    st.session_state.setdefault("quiz_submitted", set()) # set of indices
    st.session_state.setdefault("quiz_seed", 0)


def _reset_quiz(seed: int) -> None:
    st.session_state.quiz_started = True
    st.session_state.quiz_index = 0
    st.session_state.quiz_answers = {}
    st.session_state.quiz_submitted = set()
    st.session_state.quiz_seed = seed


def _stop_quiz() -> None:
    st.session_state.quiz_started = False
    st.session_state.quiz_index = 0
    st.session_state.quiz_answers = {}
    st.session_state.quiz_submitted = set()


def _coerce_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort compat shim for older runs:
    - v1 had question_text/correct_answer/distractors on each exercise.
    - v2 uses typed exercises + per-lesson flashcards.
    """
    schema_version = str(data.get("schema_version") or "v1")
    if schema_version == "v2":
        return data

    # Only attempt conversion if it *looks* like v1.
    if "modules" not in data:
        return data

    for mod in data.get("modules", []):
        for lesson in mod.get("lessons", []):
            # Add empty flashcards if missing
            lesson.setdefault("flashcards", [])
            exercises = lesson.get("exercises", [])
            new_exercises: list[dict[str, Any]] = []
            for ex in exercises:
                if "question_type" in ex and "prompt" in ex:
                    new_exercises.append(ex)
                    continue

                prompt = ex.get("question_text", "")
                correct_answer = ex.get("correct_answer", "")
                distractors = ex.get("distractors", []) or []

                options: list[dict[str, Any]] = []
                options.append({"text": str(correct_answer), "is_correct": True, "error_type": None, "feedback": None})
                for d in distractors:
                    fb = d.get("feedback")
                    options.append(
                        {
                            "text": str(d.get("text", "")),
                            "is_correct": False,
                            "error_type": d.get("error_type") or "distractor",
                            "feedback": fb,
                        }
                    )

                # Pad/truncate to 4 options to match v2 UI expectations.
                options = options[:4]
                while len(options) < 4:
                    options.append(
                        {
                            "text": "None of the above.",
                            "is_correct": False,
                            "error_type": "filler",
                            "feedback": None,
                        }
                    )

                new_exercises.append(
                    {
                        "blooms_level": ex.get("blooms_level"),
                        "question_type": "single_choice",
                        "prompt": str(prompt),
                        "options": options,
                        "feedback_for_correct": ex.get("feedback_for_correct"),
                    }
                )
            lesson["exercises"] = new_exercises

    data["schema_version"] = "v2"
    return data


def _normalize_text(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _accepted_match(user: str, accepted: list[str]) -> bool:
    u = _normalize_text(user)
    return any(u == _normalize_text(a) for a in accepted)


def _render_exercise_browse(ex: Any) -> None:
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


def _render_feedback_block(is_correct: bool, correct_feedback: Optional[str], incorrect_feedback: Optional[Feedback|str], rationale: Optional[str] = None, correct_answer_label: Optional[str] = None, correct_answer_rationale: Optional[str] = None):
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


def _render_exercise_quiz(ex: Any, *, idx: int, seed: int) -> None:
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
            options = _choice_options_for_exercise(ex, seed=seed)
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
            options = _choice_options_for_exercise(ex, seed=seed)
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
                is_correct = all(_accepted_match(prev_vals[i], gaps[i].accepted_answers) for i in range(len(gaps)))
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
        _render_feedback_block(is_correct, correct_feedback, incorrect_feedback, rationale, correct_answer_label, correct_answer_rationale)

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


def main() -> None:
    st.set_page_config(page_title="Techlingo Run Viewer", layout="wide")
    _ensure_state()

    st.title("Techlingo Run Viewer + Quiz")

    outputs_dir = _ROOT / "outputs"
    runs = _list_runs(outputs_dir)

    with st.sidebar:
        st.header("Load a run")

        run_names = ["(manual path)"] + [r.name for r in runs]
        pick = st.selectbox("Run folder", run_names, index=0)
        if pick != "(manual path)":
            st.session_state.selected_run_dir = str(outputs_dir / pick)

        run_dir_str = st.text_input("Run path", value=st.session_state.selected_run_dir, placeholder="outputs/run-YYYYMMDD-HHMMSS")
        st.session_state.selected_run_dir = run_dir_str

        st.divider()
        st.caption("Tip: pick `outputs/run-20251212-194933` to view your latest run.")

    run_dir = Path(st.session_state.selected_run_dir) if st.session_state.selected_run_dir else None
    if not run_dir or not run_dir.exists():
        st.info("Select a run folder in the sidebar to begin.")
        return

    try:
        course = _load_course(run_dir)
    except Exception as e:
        st.error(f"Failed to load course: {e}")
        return

    flat = _flatten_exercises(course)
    st.caption(
        f"Loaded: `{run_dir}` • Difficulty: `{course.difficulty.value}` • Modules: {len(course.modules)} • Exercises: {len(flat)}"
    )

    tab_browse, tab_quiz = st.tabs(["Browse", "Quiz (full course)"])

    with tab_browse:
        left, right = st.columns([0.4, 0.6], gap="large")
        lesson = None

        with left:
            st.subheader("Course outline")
            mod_titles = [m.title for m in course.modules]
            
            if not mod_titles:
                st.info("No modules in this course.")
            else:
                mod_i = st.selectbox("Module", list(range(len(mod_titles))), format_func=lambda i: mod_titles[i])
                
                if mod_i is not None:
                    lesson_titles = [l.title for l in course.modules[mod_i].lessons]
                    if not lesson_titles:
                        st.info("No lessons in this module.")
                    else:
                        lesson_i = st.selectbox("Lesson", list(range(len(lesson_titles))), format_func=lambda i: lesson_titles[i])
                        if lesson_i is not None:
                            lesson = course.modules[mod_i].lessons[lesson_i]
                            st.markdown(f"**SLO:** {lesson.slo}")
                            st.markdown(f"**Exercises:** {len(lesson.exercises)}")

            with st.expander("Pipeline artifacts (A1–A5)", expanded=False):
                artifacts_dir = run_dir / "artifacts"
                if artifacts_dir.exists():
                    artifacts = sorted(artifacts_dir.glob("*.json"))
                else:
                    artifacts = []
                if not artifacts:
                    st.caption("No artifacts found in this run.")
                else:
                    art_choice = st.selectbox("Artifact file", artifacts, format_func=lambda p: p.name)
                    if art_choice:
                        st.code(_load_json_preview(art_choice), language="json")

        with right:
            if lesson:
                st.subheader("Exercises")
                for ei, ex in enumerate(lesson.exercises):
                    header = f"{ei+1}. {ex.blooms_level.value}"
                    with st.expander(header, expanded=(ei == 0)):
                        _render_exercise_browse(ex)

                st.subheader("Flashcards")
                if lesson.flashcards:
                    for fi, fc in enumerate(lesson.flashcards):
                        with st.expander(f"{fi+1}. {fc.front}", expanded=(fi == 0)):
                            st.markdown(f"**Front:** {fc.front}")
                            st.markdown(f"**Back:** {fc.back}")
                            if fc.hint:
                                st.caption(f"Hint: {fc.hint}")
                else:
                    st.caption("No flashcards present.")
            else:
                st.info("Select a lesson to view details.")

    with tab_quiz:
        st.subheader("Full-course quiz")
        st.caption("Runs through every exercise sequentially. Your answers are kept locally in this browser session.")

        seed = abs(hash(str(run_dir))) % (2**31 - 1)

        cols = st.columns([0.25, 0.25, 0.5])
        with cols[0]:
            if st.button("Start / Restart quiz", type="primary"):
                _reset_quiz(seed=seed)
        with cols[1]:
            if st.button("End quiz"):
                _stop_quiz()

        if not st.session_state.quiz_started:
            st.info("Click **Start / Restart quiz** to begin.")
            return

        idx = int(st.session_state.quiz_index)
        idx = max(0, min(idx, len(flat) - 1))
        st.session_state.quiz_index = idx

        ex = flat[idx]
        st.progress((idx + 1) / max(1, len(flat)))
        st.markdown(
            f"**{idx+1}/{len(flat)}** • **{ex['module_title']} → {ex['lesson_title']}**\n\n"
            f"**Bloom:** {ex['blooms_level']}\n\n"
            f"**SLO:** {ex['slo']}"
        )
        ex_obj = cast(Any, ex["exercise"])
        st.markdown(f"### {getattr(ex_obj, 'prompt', '')}")
        st.caption(f"Type: `{getattr(ex_obj, 'question_type', 'unknown')}`")

        _render_exercise_quiz(ex_obj, idx=idx, seed=st.session_state.quiz_seed + idx)
        
        # Navigation handled in _render_exercise_quiz, but we keep Back and Finish
        nav = st.columns([0.2, 0.8])
        with nav[0]:
            if st.button("Back", disabled=(idx == 0)):
                st.session_state.quiz_index = max(0, idx - 1)
                st.rerun()
        
        with nav[1]:
            # Show "Finish" only if on last question and answered?
            # Or just always show?
            if st.button("Finish + Review", disabled=(len(st.session_state.quiz_answers) < len(flat))):
                st.session_state.quiz_index = len(flat)  # sentinel
                st.rerun()

        # Review screen
        if st.session_state.quiz_index == len(flat):
            st.markdown("## Review")
            correct = 0
            for i, ex2 in enumerate(flat):
                ex_obj2 = cast(Any, ex2["exercise"])
                _, fb2 = _render_exercise_quiz(ex_obj2, idx=i, seed=st.session_state.quiz_seed + i) if False else (False, None)
                # We don't re-render interactive controls in review; we compute correctness from stored answers.
                stored = st.session_state.quiz_answers.get(str(i))
                qtype = getattr(ex_obj2, "question_type", "unknown")

                def _grade() -> tuple[bool, Optional[Feedback], str]:
                    if isinstance(ex_obj2, SingleChoiceExercise):
                        if not isinstance(stored, str):
                            return False, None, "(missing)"
                        # Rebuild deterministic shuffled options to interpret stored id.
                        opts = _choice_options_for_exercise(ex_obj2, seed=st.session_state.quiz_seed + i)
                        picked = next((o for o in opts if o.id == stored), None)
                        if not picked:
                            return False, None, "(missing)"
                        return picked.is_correct, (picked.feedback if (picked and not picked.is_correct) else None), picked.label

                    if isinstance(ex_obj2, MultiChoiceExercise):
                        if not isinstance(stored, list):
                            return False, None, "(missing)"
                        opts = _choice_options_for_exercise(ex_obj2, seed=st.session_state.quiz_seed + i)
                        opt_by_id = {o.id: o for o in opts}
                        picked_labels = [opt_by_id[s].label for s in stored if s in opt_by_id]
                        correct_ids = {o.id for o in opts if o.is_correct}
                        is_ok = set(stored) == correct_ids
                        fb = None
                        if not is_ok:
                            wrong = next((opt_by_id[s] for s in stored if s in opt_by_id and not opt_by_id[s].is_correct), None)
                            fb = wrong.feedback if wrong else None
                        return is_ok, fb, ", ".join(picked_labels) if picked_labels else "(missing)"

                    if isinstance(ex_obj2, TrueFalseExercise):
                        if not isinstance(stored, bool):
                            return False, None, "(missing)"
                        is_ok = stored == ex_obj2.correct_answer
                        fb = ex_obj2.feedback_for_incorrect if (not is_ok and isinstance(ex_obj2.feedback_for_incorrect, Feedback)) else None
                        return is_ok, fb, "True" if stored else "False"

                    if isinstance(ex_obj2, FillGapsExercise):
                        gaps2: list[FillGapsGapPart] = [p for p in ex_obj2.parts if isinstance(p, FillGapsGapPart)]
                        if not isinstance(stored, list) or len(stored) != len(gaps2):
                            return False, None, "(missing)"
                        is_ok = all(_accepted_match(stored[j], gaps2[j].accepted_answers) for j in range(len(gaps2)))
                        return is_ok, None, " | ".join(stored)

                    if isinstance(ex_obj2, RearrangeExercise):
                        if not isinstance(stored, list):
                            return False, None, "(missing)"
                        is_ok = stored == ex_obj2.correct_order
                        return is_ok, None, " | ".join(stored) if stored else "(missing)"

                    return False, None, "(missing)"

                is_ok, fb, your_answer = _grade()
                correct += 1 if is_ok else 0

                with st.expander(f"{i+1}. {'✅' if is_ok else '❌'} {ex2['module_title']} → {ex2['lesson_title']}"):
                    st.markdown(f"**Type:** `{qtype}`")
                    st.markdown(f"**Prompt:** {getattr(ex_obj2, 'prompt', '')}")
                    st.markdown(f"**Your answer:** {your_answer}")
                    if isinstance(ex_obj2, TrueFalseExercise):
                        st.markdown(f"**Correct:** {'True' if ex_obj2.correct_answer else 'False'}")
                    if fb:
                        st.markdown(f"**Intrinsic:** {fb.intrinsic}")
                        st.markdown(f"**Instructional:** {fb.instructional}")

            st.success(f"Score: {correct}/{len(flat)}")


if __name__ == "__main__":
    main()


