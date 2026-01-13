from __future__ import annotations

from textwrap import dedent
from typing import Any
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
    if difficulty == DifficultyLevel.novice:
        return dedent(
            """\
            Difficulty: novice
            - Use extremely simple, self-evident language.
            - Focus on basic recognition and obvious facts.
            - Ensure questions are very easy, almost trivial.
            - No technical jargon whatsoever.
            - Scenarios should be very simple and everyday.
            """
        )
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


def a1_modularizer_prompt(source_text: str, *, difficulty: DifficultyLevel, config: WorkflowConfig, override_title: str | None = None) -> str:
    target_title = override_title if override_title else "AI Core Capabilities and Responsibility"
    
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Source Content:
        {source_text}

        Task (A1 Modularizer - Curriculum Mapping & Chunking):
        Create a course map for: "{target_title}".

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.
        
        STRICT CONSTRAINT: You must cover ALL subjects, terms, and parts present in the source text. Do not miss any concepts.


        Constraints:
        - Exactly {config.modules_count} modules.
        - Total lessons across all modules: {config.min_lessons_total} to {config.max_lessons_total}.
        - Each lesson must have exactly one SLO (single, clear, measurable learning objective).
        - Keep lesson titles and SLOs novice-friendly (no unexplained jargon).

        Output JSON schema:
        {{
          "title": "{target_title}",
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


def a2_scaffolder_prompt(
    course_map_json: str, 
    *, 
    difficulty: DifficultyLevel, 
    config: WorkflowConfig, 
    override_title: str | None = None,
    validation_issues: list[dict[str, Any]] | None = None
) -> str:
    target_title = override_title if override_title else "AI Core Capabilities and Responsibility"
    blooms_reqs = "\n".join([f"- {k}: {v} exercises" for k, v in config.blooms_distribution.items()])
    type_reqs = "\n".join([f"    - {k}: {v}" for k, v in config.question_type_distribution.items()])

    # Construct feedback section if issues exist
    feedback_section = ""
    if validation_issues:
        issues_str = "\n".join([f"- {i['severity'].upper()} at {i['path']}: {i['message']}" for i in validation_issues])
        feedback_section = dedent(f"""
        CRITICAL INSTRUCTION - PREVIOUS ATTEMPT FAILED VALIDATION
        Your previous output had the following errors. You MUST fix them in this new attempt:
        {issues_str}
        
        Refuse to generate the same broken content again.
        """)

    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        {feedback_section}

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
          - flashcards: exactly {config.flashcards_per_lesson} items.
            - STRICT CONSTRAINT: Atomic & Concise. Each flashcard must cover exactly ONE simple concept.
            - Front: specific term, question, or scenario (max 10 words).
            - Back: clear, direct definition or answer (max 15 words).
            - FORBIDDEN: Do NOT generate "summaries", "lists of items", or "overview" cards.
            - FORBIDDEN: Do NOT enable "List 3 types of..." style cards.
            - Good: "What is X?" -> "X is ..."
            - Good: "Action for X?" -> "Do Y."
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
          - include at least 1 gap part.
          - STRICT CONSTRAINT: Semantic Coherence. The text surrounding the gap MUST provide enough context so that the accepted answer is the only logical choice. Do not create gaps where any random noun could fit.
        - rearrange:
          - word_bank: list of tokens (words or short phrases).
          - correct_order: list of tokens in correct order.
          - correct_order must use the same tokens as word_bank.
          - Task must be "Reconstruct the sentence" or "Order the steps". Do NOT use scenarios for this type.

        Output JSON schema (must be valid and complete):
        {{
          "title": "{target_title}",
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
        Rewrite exercises to ensure contextual relevance and stylistic variety:
        
        1. **Higher-Order Thinking (Applying, Analyzing/Evaluating)**:
           - MUST be scenario-based (EXCEPT 'rearrange' and 'fill_gaps').
           - Include a brief scenario + clear decision point/problem.
        
        2. **Lower-Order Thinking (Remembering, Understanding)**:
           - MUST be **DIRECT** questions (NO "Scenario:" prefix, NO "You are a..." framing).
           - Focus on clear, concise concept checking.
           - Exception: If the concept is abstract, a very brief example is okay, but avoid full role-play scenarios.
        
        3. **Specific Type Constraints**:
           - **rearrange**: Do NOT use scenarios. Prompt should be "Arrange the following steps..." or "Reconstruct the sentence...".
           - **fill_gaps**: Do NOT use scenarios. Prompt should be a direct statement with missing key terms. Ensure the sentence makes sense grammatically even with the gap.

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        Scenario requirements (for Applying/Analyzing only, excluding rearrange/fill_gaps):
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
            - intrinsic: realistic consequence within the context of the scenario (never a software system error unless the context IS software systems).
            - instructional: conversational coaching that explains the violated principle.

        For true_false exercises:
        - Add 'feedback_for_correct' (1-2 sentences).
        - Add 'feedback_for_incorrect' object (intrinsic + instructional) explaining why the user's choice was wrong.

        For fill_gaps / rearrange:
        - Add 'feedback_for_correct' (brief reinforcement).

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge. Verify that every explanation can be pointed to in the source text.
        
        NEGATIVE CONSTRAINTS:
        - Do NOT use terms like 'SLO', 'learning objective', 'system', 'tool', or 'AI'.
        - Do NOT frame feedback as 'The system will...' or 'The tool suggests...'.
        - Ensure feedback sounds like a human mentor, not a software debugger.

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
        4. Check 'fill_gaps' exercises: does the sentence make grammatical sense when filled? Is the context sufficient to derive the answer?
        5. Check 'rearrange' exercises: is the "correct order" a valid, logical sentence or sequence?

        Output JSON schema:
        {{
          "issues": [
            {{
              "path": "modules[0].lessons[0].exercises[0]",
              "message": "Answer relies on external knowledge... OR Fill-gap sentence is grammatically broken... OR Rearrange order is illogical..."
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
        - Every Applying and Analyzing/Evaluating exercise must be scenario-based (EXCEPT rearrange/fill_gaps).
        - For scenario-based single_choice and multi_choice exercises: each incorrect option must include paired feedback (intrinsic + instructional).
        - For single_choice and multi_choice exercises:
          - ALL options must have a 'rationale' (2-3 sentences explaining why it is correct/incorrect).
          - ALL incorrect options must have a 'better_fit' (1-2 sentences describing where it would be correct).
        - For scenario-based true_false exercises: feedback_for_incorrect must be present (intrinsic + instructional).
        - For fill_gaps: Ensure grammatical correctness and semantic coherence (no "guess the noun" games).
        - For rearrange: Ensure the final order forms a logical sentence or process step. DO NOT use scenarios.

        Validation issues:
        {issues_json}
        
        Current course JSON:
        {bad_course_json}
        """
    )



def analyzer_prompt(source_text: str) -> str:
    return dedent(
        f"""\
        You are an expert Text Analyst and Linguist.
        Your goal is to break down the source text into its constituent parts to ensure absolutely NOTHING is missed.
        
        Source Text:
        {source_text}
        
        Task:
        Analyze the text and identify EVERY:
        - Term (key vocabulary)
        - Definition (explicit or implicit)
        - Explanation (how things work, why they matter)
        - Example (illustrations of concepts)
        - Analogy (comparisons)
        - Subject (main topics)

        You must also recommend a course structure (Workflow Config) based on the depth and breadth of the content.
        - modules_count: typically 1 (since input is usually a single module/unit). Only suggest >1 if content is massive and clearly distinct sections.
        - lessons_total: typically 5-15 depending on content length.
        - exercises_per_lesson: typically 15-30 (as requested for robust practice).
        - flashcards_per_lesson: typically 6-10
        - blooms_distribution: how many of each level per lesson (Remembering/Understanding/Applying/Analyzing/Evaluating)
        - question_type_distribution: exact mix of question types per lesson. 
          STRICT CONSTRAINTS:
          1. Must sum EXACTLY to exercises_per_lesson.
          2. Use ONLY these keys: "single_choice", "multi_choice", "true_false", "fill_gaps", "rearrange".
          3. DO NOT use "short_answer" or any other keys.

        
        Output JSON schema:
        {{
            "input_summary": "Brief summary of the text",
            "parts": [
                {{
                    "type": "term|definition|explanation|example|analogy|subject",
                    "content": "The actual text snippet or summary of the part",
                    "context": "Optional context if needed"
                }}
            ],
            "metadata": {{
                "total_parts": 0,
                "parts_by_type": {{
                    "term": 0,
                    "definition": 0,
                    "explanation": 0,
                    "example": 0,
                    "analogy": 0,
                    "subject": 0
                }},
                "estimated_questions_needed": 0,
                "completeness_score": 0.0
            }},
            "recommended_config": {{
                "difficulty": "beginner|intermediate|advanced",
                "modules_count": 0,
                "min_lessons_total": 0,
                "max_lessons_total": 0,
                "exercises_per_lesson": 0,
                "flashcards_per_lesson": 0,
                "blooms_distribution": {{
                    "Remembering": 0,
                    "Understanding": 0,
                    "Applying": 0,
                    "Analyzing/Evaluating": 0
                }},
                "question_type_distribution": {{
                    "single_choice": 0,
                    "multi_choice": 0,
                    "true_false": 0,
                    "fill_gaps": 0,
                    "rearrange": 0
                }}
            }}
        }}
        
        STRICT CONSTRAINT FOR 'difficulty':
        You MUST pick exactly ONE value: "beginner", "intermediate", or "advanced".
        Do NOT combine them (e.g., "beginner-to-intermediate" is INVALID).
        pick the single closest difficulty level.
        """
    )


def reviewer_prompt(source_text: str, current_analysis_json: str) -> str:
    return dedent(
        f"""\
        You are a meticulous Content Reviewer.
        Your goal is to check if the previous analysis missed ANYTHING from the source text.
        
        Source Text:
        {source_text}
        
        Current Analysis:
        {current_analysis_json}
        
        Task:
        1. Compare the Source Text against the Current Analysis.
        2. Identify any missing Terms, Definitions, Explanations, Examples, Analogies, or Subjects.
        3. If items are missing, add them.
        4. If items are incorrect, fix them.
        6. Verify and Refine the `recommended_config`:
           - Ensure it pushes for deep learning (e.g., higher Bloom's levels).
           - Ensure exercises_per_lesson is 15-30.
           - Ensure question_type_distribution sums EXACTLY to exercises_per_lesson.
           - Ensure question_type_distribution ONLY uses keys: "single_choice", "multi_choice", "true_false", "fill_gaps", "rearrange".
           - REMOVE any invalid keys like "short_answer".
        
        STRICT CONSTRAINT FOR 'difficulty':
        Ensure 'difficulty' is exactly one of: "beginner", "intermediate", "advanced".
        If it contains a range like "beginner-to-intermediate", CHANGE it to the single highest level (e.g., "intermediate").
        
        Output the FULL validated/corrected JSON using the same schema.
        """
    )
