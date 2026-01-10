from __future__ import annotations

from textwrap import dedent
from .config import WorkflowConfig, DifficultyLevel


SYSTEM_JSON_ONLY = dedent(
    """\
    You are Techlingo-ID-Architect, an expert Instructional Designer and Duolingo content specialist.
    You MUST follow instructions precisely.

    Output rules:
    - Return ONLY valid JSON.
    - Do NOT include markdown fences.
    - Do NOT include commentary or explanations.
    """
)


def difficulty_contract(difficulty: DifficultyLevel) -> str:
    if difficulty == DifficultyLevel.beginner:
        return dedent(
            """\
            Difficulty: beginner
            - Use simple language; avoid jargon unless defined inline.
            - Keep questions short and concrete.
            - Scenarios should be everyday workplace situations with minimal ambiguity.
            - Distractors should be common novice mistakes.
            - Feedback should be encouraging and explanatory.
            """
        )
    if difficulty == DifficultyLevel.intermediate:
        return dedent(
            """\
            Difficulty: intermediate
            - Moderate technical vocabulary is allowed; define uncommon terms briefly.
            - Scenarios should be realistic implementation tasks with some tradeoffs.
            - Distractors should reflect plausible misconceptions, not silly errors.
            - Feedback should highlight the key principle and the tradeoff.
            """
        )
    return dedent(
        """\
        Difficulty: advanced
        - Use more technical phrasing appropriate for practitioners (still clear and unambiguous).
        - Scenarios should involve operational constraints, governance, or failure modes.
        - Distractors should be subtle and realistic (edge cases, misapplied best practices).
        - Feedback should be precise and principle-driven.
        """
    )


def a1_modularizer_prompt(source_text: str, *, difficulty: DifficultyLevel, config: WorkflowConfig) -> str:
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Source Content:
        {source_text}

        Task (A1 Modularizer - Curriculum Mapping & Chunking):
        Create a course map for: "AI Core Capabilities and Responsibility".

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.
        
        STRICT CONSTRAINT: You must cover ALL subjects, terms, and parts present in the source text. Do not miss any concepts.


        Constraints:
        - Exactly {config.modules_count} modules.
        - Total lessons across all modules: {config.min_lessons_total} to {config.max_lessons_total}.
        - Each lesson must have exactly one SLO (single, clear, measurable learning objective).
        - Keep lesson titles and SLOs novice-friendly (no unexplained jargon).

        Output JSON schema:
        {{
          "title": "AI Core Capabilities and Responsibility",
          "modules": [
            {{
              "title": "Module Title",
              "lessons": [
                {{
                  "title": "Lesson Title",
                  "slo": "Single measurable objective"
                }}
              ]
            }}
          ]
        }}
        """
    )


def a2_scaffolder_prompt(course_map_json: str, *, difficulty: DifficultyLevel, config: WorkflowConfig) -> str:
    blooms_reqs = "\n".join([f"- {k}: {v} exercises" for k, v in config.blooms_distribution.items()])
    type_reqs = "\n".join([f"    - {k}: {v}" for k, v in config.question_type_distribution.items()])

    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Input course map JSON:
        {course_map_json}

        Task (A2 Scaffolder - Q&A Generator):
        For EACH lesson SLO, generate exactly {config.exercises_per_lesson} exercises with vertical progression using Bloom's Taxonomy:
        {blooms_reqs}

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.
        STRICT CONSTRAINT: Verify that the exercises cover all subjects/terms defined in the lesson SLOs derived from the text.


        Constraints:
        - Each lesson must include:
          - exercises: exactly {config.exercises_per_lesson} items, with this exact per-lesson mix:
        {type_reqs}
          - flashcards: exactly {config.flashcards_per_lesson} items
        - Every exercise must include:
          - blooms_level (one of: Remembering, Understanding, Applying, Analyzing/Evaluating)
          - question_type (one of: single_choice, multi_choice, true_false, fill_gaps, rearrange)
          - prompt (learner-facing prompt; may include scenario/context)
        - For Applying and Analyzing/Evaluating exercises, the prompt must clearly include a scenario and a decision point.
        - Keep answers concise and unambiguous.
        - single_choice:
          - options: 4 options, each has text + is_correct + (error_type for incorrect options)
          - exactly 1 option where is_correct=true
        - multi_choice:
          - options: 4 options, each has text + is_correct + (error_type for incorrect options)
          - 2 or 3 options where is_correct=true
          - Set option feedback/rationale/better_fit fields to null in A2 (they will be added later).
        - true_false:
          - statement: the statement to judge
          - correct_answer: true/false
        - fill_gaps:
          - parts: array of objects with discriminator field 'type'
            - {{"type":"text","text":"..."}}
            - {{"type":"gap","accepted_answers":["..."],"placeholder":"..."}}
          - include at least 1 gap part
        - rearrange:
          - word_bank: list of tokens
          - correct_order: list of tokens in correct order
          - correct_order must use the same tokens as word_bank

        Output JSON schema (must be valid and complete):
        {{
          "title": "AI Core Capabilities and Responsibility",
          "modules": [
            {{
              "title": "Module Title",
              "lessons": [
                {{
                  "title": "Lesson Title",
                  "slo": "SLO",
                  "exercises": [
                    {{
                      "blooms_level": "Remembering|Understanding|Applying|Analyzing/Evaluating",
                      "question_type": "single_choice|multi_choice|true_false|fill_gaps|rearrange",
                      "prompt": "...",

                      "options": [
                        {{ "text": "...", "is_correct": true, "error_type": null, "feedback": null, "rationale": null, "better_fit": null }},
                        {{ "text": "...", "is_correct": false, "error_type": "...", "feedback": null, "rationale": null, "better_fit": null }}
                      ],

                      "statement": "...",
                      "correct_answer": true,

                      "parts": [
                        {{ "type": "text", "text": "..." }},
                        {{ "type": "gap", "accepted_answers": ["..."], "placeholder": "..." }}
                      ],

                      "word_bank": ["..."],
                      "correct_order": ["..."]
                    }}
                  ],
                  "flashcards": [
                    {{ "front": "...", "back": "...", "hint": "..." }}
                  ]
                }}
              ]
            }}
          ]
        }}
        """
    )


def a3_scenario_designer_prompt(course_json: str, *, difficulty: DifficultyLevel, config: WorkflowConfig) -> str:
    blooms_counts = "/".join([str(v) for v in config.blooms_distribution.values()])
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Input course JSON:
        {course_json}

        Task (A3 Merrill’s Agent - Scenario Designer):
        Rewrite exercises to ensure contextual relevance:
        - Every Applying and Analyzing/Evaluating exercise MUST be scenario-based (include scenario + clear decision point in prompt).
        - Preferably make Understanding exercises scenario-based too (when natural).

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        Scenario requirements (for scenario-based items):
        - Problem-centered trigger event
        - Relatable protagonist (role/title)
        - Clear decision point in the question

        Constraints:
        - Do not change lesson/module structure.
        - Preserve Bloom level counts per lesson ({blooms_counts}).
        - Preserve each exercise's question_type and required fields.
        - For fill_gaps and rearrange, keep the structure valid (parts/word_bank/correct_order).
        - Do not add or remove exercises.
        - Do not add or remove flashcards.
        - Keep the correct answer semantically correct.

        Output: return the FULL updated course JSON (same schema as input).
        """
    )


def a4_feedback_architect_prompt(course_json: str, *, difficulty: DifficultyLevel, config: WorkflowConfig) -> str:
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Input course JSON:
        {course_json}

        Task (A4 Feedback Architect - Instructional Coaching):
        You must populate all feedback fields for every exercise.
        
        For ALL single_choice and multi_choice exercises:
        - Add 'feedback_for_correct' (1-2 sentences reinforcing why the answer is right).
        - Add a 'rationale' (2-3 sentences) for EVERY option (both correct and incorrect).
        - Add a 'better_fit' (1-2 sentences) for EVERY incorrect option.
        - For EVERY incorrect option, you MUST add a 'feedback' object with:
            - intrinsic: realistic consequence/system reaction to the error.
            - instructional: conversational coaching that explains the violated principle.

        For true_false exercises:
        - Add 'feedback_for_correct' (1-2 sentences).
        - Add 'feedback_for_incorrect' object (intrinsic + instructional) explaining why the user's choice was wrong.

        For fill_gaps / rearrange:
        - Add 'feedback_for_correct' (brief reinforcement).

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        Constraints:
        - Populate 'rationale' for all options.
        - Populate 'better_fit' for all incorrect options.
        - Ensure 'feedback' object (intrinsic + instructional) is present for ALL incorrect options in single/multi choice.
        - Ensure 'feedback_for_correct' is present for ALL exercises.
        - Ensure 'feedback_for_incorrect' is present for ALL true_false exercises.
        - Do not remove existing fields.
        - Keep feedback/rationale concise and learner-friendly.
        - Do not add or remove exercises or flashcards.

        Output: return the FULL updated course JSON.
        """
    )


def a5_source_check_prompt(course_json: str, source_text: str) -> str:
    return dedent(
        f"""\
        You are a strict Fact Checker and Editor.
        Your goal is to identify exercises where the correct answer relies on external knowledge NOT present in the source text.
        Even if a question is factually correct in the real world, if the source text does not support it, it is a HALLUCINATION.

        Source Text:
        {source_text}

        Course Content:
        {course_json}

        Task:
        1. Review every exercise in the course.
        2. Check if the CORRECT ANSWER is explicitly supported by the Source Text.
        3. If the answer requires outside knowledge, flag it.

        Output JSON schema:
        {{
          "issues": [
            {{
              "path": "modules[0].lessons[0].exercises[0]",
              "message": "Answer relies on external knowledge about X, which is not in the source text."
            }}
          ]
        }}

        Return ONLY valid JSON. If no issues found, return {{ "issues": [] }}.
        """
    )


def a5_repair_prompt(bad_course_json: str, issues_json: str, config: WorkflowConfig) -> str:
    blooms_reqs = ", ".join([f"{v} {k}" for k, v in config.blooms_distribution.items()])
    type_reqs = "\n".join([f"          - {k}: {v}" for k, v in config.question_type_distribution.items()])

    return dedent(
        f"""\
        You must repair the course JSON to satisfy all constraints.
        Return ONLY corrected JSON.

        Constraints to satisfy:
        - Exactly {config.modules_count} modules.
        - Total lessons across modules: {config.min_lessons_total} to {config.max_lessons_total}.
        - Each lesson has exactly {config.exercises_per_lesson} exercises.
        - Bloom distribution per lesson: {blooms_reqs}.
        - Exercise type mix per lesson (exact counts within the {config.exercises_per_lesson} exercises):
{type_reqs}
        - Each lesson has exactly {config.flashcards_per_lesson} flashcards.
        - Every Applying and Analyzing/Evaluating exercise must be scenario-based (prompt must clearly describe a scenario + decision point).
        - For scenario-based single_choice and multi_choice exercises: each incorrect option must include paired feedback (intrinsic + instructional).
        - For single_choice and multi_choice exercises:
          - ALL options must have a 'rationale' (2-3 sentences explaining why it is correct/incorrect).
          - ALL incorrect options must have a 'better_fit' (1-2 sentences describing where it would be correct).
        - For scenario-based true_false exercises: feedback_for_incorrect must be present (intrinsic + instructional).

        Validation issues:
        {issues_json}

        Current course JSON:
        {bad_course_json}
        """
    )


