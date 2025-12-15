from __future__ import annotations

from textwrap import dedent

from .models import DifficultyLevel


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


def a1_modularizer_prompt(source_text: str, *, difficulty: DifficultyLevel) -> str:
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Source Content:
        {source_text}

        Task (A1 Modularizer - Curriculum Mapping & Chunking):
        Create a course map for: "AI Core Capabilities and Responsibility".

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        Constraints:
        - Exactly 6 modules.
        - Total lessons across all modules: 20 to 25.
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


def a2_scaffolder_prompt(course_map_json: str, *, difficulty: DifficultyLevel) -> str:
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Input course map JSON:
        {course_map_json}

        Task (A2 Scaffolder - Q&A Generator):
        For EACH lesson SLO, generate exactly 8 exercises with vertical progression using Bloom's Taxonomy:
        - Remembering: 2 exercises
        - Understanding: 2 exercises
        - Applying: 2 exercises
        - Analyzing/Evaluating: 2 exercises

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        Constraints:
        - Each lesson must include:
          - exercises: exactly 8 items, with this exact per-lesson mix:
            - single_choice: 1
            - multi_choice: 2
            - true_false: 2
            - fill_gaps: 2
            - rearrange: 1
          - flashcards: exactly 8 items
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


def a3_scenario_designer_prompt(course_json: str, *, difficulty: DifficultyLevel) -> str:
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
        - Preserve Bloom level counts per lesson (2/2/2/2).
        - Preserve each exercise's question_type and required fields.
        - For fill_gaps and rearrange, keep the structure valid (parts/word_bank/correct_order).
        - Do not add or remove exercises.
        - Do not add or remove flashcards.
        - Keep the correct answer semantically correct.

        Output: return the FULL updated course JSON (same schema as input).
        """
    )


def a4_feedback_architect_prompt(course_json: str, *, difficulty: DifficultyLevel) -> str:
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Input course JSON:
        {course_json}

        Task (A4 Feedback Architect - Instructional Coaching):
        For ALL single_choice and multi_choice exercises:
        - Add a 'rationale' (2-3 sentences) for EVERY option (both correct and incorrect).
        - Add a 'better_fit' (1-2 sentences) for EVERY incorrect option.

        For scenario-based exercises (Applying and Analyzing/Evaluating):
        - Also ensure scenario-based feedback is present (as before).
        - For incorrect responses:
          - intrinsic: realistic consequence/system reaction
          - instructional: conversational coaching that explains the violated principle and remediation

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        Constraints:
        - For single_choice and multi_choice exercises:
          - Populate 'rationale' for all options.
            - Why is this option correct or incorrect in this specific context?
          - Populate 'better_fit' for all incorrect options (is_correct=false).
            - Describe a context where this option WOULD be the correct answer (e.g., "This would be correct if you were optimizing for X instead of Y").
          - Preserve existing 'feedback' objects if present (or add them if missing for scenario-based incorrect options).
        - For true_false exercises: set feedback_for_incorrect (if scenario-based).
        - Feedback must be an object with keys intrinsic + instructional.
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


def a5_repair_prompt(bad_course_json: str, issues_json: str) -> str:
    return dedent(
        f"""\
        You must repair the course JSON to satisfy all constraints.
        Return ONLY corrected JSON.

        Constraints to satisfy:
        - Exactly 6 modules.
        - Total lessons across modules: 20 to 25.
        - Each lesson has exactly 8 exercises.
        - Bloom distribution per lesson: 2 Remembering, 2 Understanding, 2 Applying, 2 Analyzing/Evaluating.
        - Exercise type mix per lesson (exact counts within the 8 exercises):
          - single_choice: 1
          - multi_choice: 2
          - true_false: 2
          - fill_gaps: 2
          - rearrange: 1
        - Each lesson has exactly 8 flashcards.
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


